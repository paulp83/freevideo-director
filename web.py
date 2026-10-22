#!/usr/bin/env python3
"""Browser UI for the FreeVideo director.  http://127.0.0.1:8192

Idea + images -> H3 prompt (director.direct) -> render on the FreeVideo ComfyUI (:8190) ->
optional Final finishing (enhance.py runs the project's finishing.py settings: upscaling,
frame rate, encoding quality). Every render is a
job with visible stages and live progress (ComfyUI websocket). Stdlib only.
"""
import argparse, base64, json, os, re, socket, struct, subprocess, threading, time, urllib.error, \
    urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import director
import finishing
import jobs_db
import library
import notify
import readiness
import stats
import enhance
import project as projects

HERE = Path(__file__).resolve().parent
COMFY = enhance.COMFY                           # config: comfy_url (--comfy-url / COMFY_URL); enhance reads the same
COMFY_OUTPUT = enhance.COMFY_ROOT / 'output'
CLIENT_ID = config.value('comfy_client_id')
FFMPEG = config.value('ffmpeg')

PRESETS = config.value('presets')
STAGES = {
    'render': 'Render',
    'upscale': 'Upscale',
    'smooth': 'Smooth',
    'encode': 'Encode',
}
# The upscale/smooth labels follow the project's finishing settings (finishing.stage_label);
# the legacy ENHANCE_NODES map only still applies to jobs resumed from pre-settings snapshots.
ENHANCE_NODES = {'1': 'upscale', '2': 'upscale', '3': 'upscale', '4': 'upscale', '5': 'smooth', '6': 'encode'}


# ---------------------------------------------------------------- ComfyUI REST

def comfy(path, data=None, headers=None):
    req = urllib.request.Request(COMFY + path, data, headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

class ComfyReject(RuntimeError):
    """ComfyUI answered POST /prompt with an explicit HTTP refusal."""


class UntrackableSubmit(Exception):
    """The durable store refused the pre-submit record; the submit must NOT go out."""


def _post_prompt(payload):
    body = json.dumps({**payload, 'client_id': CLIENT_ID}).encode()
    try:
        return comfy('/prompt', body, {'Content-Type': 'application/json'})
    except urllib.error.HTTPError as e:
        raise ComfyReject(e.read().decode(errors='replace')[:800])
    except (OSError, ValueError, KeyError) as e:
        raise AmbiguousSubmit(str(e) or type(e).__name__)


def _correlation_rejected(detail):
    """True when ComfyUI refuses our `prompt_id` field itself, not the graph: the only refusal
    where posting again is safe, because a field-level rejection accepted nothing."""
    d = detail.lower()
    return 'prompt_id' in d or 'unexpected' in d or ('invalid' in d and 'prompt' in d)


def submit_tracked(job, payload):
    """POST /prompt exactly once, with a prompt id WE supplied -- and record that id durably
    BEFORE the POST leaves. That pre-submit record is the point: if the answer is lost, the job
    turns unknown holding OUR id, and reconciliation can later ask ComfyUI whether that exact
    prompt ran. If the store cannot write the record the submit refuses (UntrackableSubmit):
    untrackable work must not happen. A backend that rejects the id field gets exactly one
    uncorrelated retry; an answer naming a different id is ambiguous (the answer does not
    describe our prompt), and the recorded id is replaced by the one the answer names."""
    pid = str(uuid.uuid4())
    job['prompt_ids'].append(pid)
    job['correlation'] = 'client-supplied'
    try:
        jobs_db.record_submit(job, pid, **db_kwargs(job))
    except jobs_db.JobDbError as e:
        job['prompt_ids'].pop()
        job['correlation'] = None
        raise UntrackableSubmit('the job store refused the pre-submit record '
                                f'({e}); refusing to submit work we could not track')
    try:
        answer = _post_prompt({**payload, 'prompt_id': pid})
    except ComfyReject as e:
        if not _correlation_rejected(str(e)):
            job['prompt_ids'].pop()          # an explicit refusal of the graph accepted nothing
            raise RuntimeError(f'ComfyUI rejected the job: {e}')
        print(f'jobs: {COMFY} refused the client-supplied prompt_id; submitting uncorrelated once')
        job['prompt_ids'].pop()
        job['correlation'] = 'server-assigned'
        answer = _post_prompt(payload)       # the fallback happens exactly once
        pid = None
    got = answer.get('prompt_id') if isinstance(answer, dict) else None
    if not got:
        raise AmbiguousSubmit('ComfyUI answered /prompt without a prompt_id;')
    if pid is not None and got != pid:
        job['correlation'] = 'server-assigned'
        job['prompt_ids'][-1] = got          # keep the id the answer actually names
        raise AmbiguousSubmit(f'ComfyUI answered with prompt id {got}, not the {pid} we submitted;')
    if not job['prompt_ids'] or job['prompt_ids'][-1] != got:
        job['prompt_ids'].append(got)        # the server-assigned path records here
    db_note(job)                             # the row keeps 'submitting' until a later transition
    return got


def upload(name, raw):
    """Upload image bytes to ComfyUI's input folder; return the stored name."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\ndirector\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
    for attempt in range(3):                   # same-name overwrite=true upload is idempotent
        try:
            r = comfy('/upload/image', body, {'Content-Type': f'multipart/form-data; boundary={boundary}'})
            break
        except urllib.error.HTTPError:
            raise                              # ComfyUI answered: retrying is pointless
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1.0)
    return f"{r['subfolder']}/{r['name']}" if r.get('subfolder') else r['name']


def clean_loras(items):
    """Validate a LoRA list -> [{'name', 'strength', 'enabled'}]; ValueError on bad input (the handler answers 400)."""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError('loras must be a list of at most 8 items')
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('each lora must be an object')
        name = item.get('name')
        if not isinstance(name, str) or not name.endswith('.safetensors') or '/' in name or '..' in name:
            raise ValueError('lora name must be a .safetensors file name')
        strength = item.get('strength', 1.0)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not -2 <= strength <= 2:
            raise ValueError('lora strength must be a number from -2 to 2')
        enabled = item.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('lora enabled must be true or false')
        out.append({'name': name, 'strength': float(strength), 'enabled': enabled})
    return out


def build_graph(prompt, seconds, width, height, seed, refs, first, last, two_pass=True,
                loras=None, uploader=None):
    """Build the exact FreeVideo workflow for `prompt` (refs are <Picture N> files; first/last are
    frames), upload the pictures, and return the dict WITHOUT submitting it: the caller records the
    graph in the job's snapshot first, then submits it as-is. `loras` is a [{name, strength, enabled}]
    list; disabled rows ride along in the node so the graph matches the project. `uploader` replaces
    the upload step (readiness probes the node set with a graph that must not reach ComfyUI)."""
    upload_picture = uploader or upload
    gen = {'text': prompt, 'width': width, 'height': height, 'seconds': float(seconds), 'seed': seed,
           'two_pass': two_pass, 'base_steps': 8, 'refine_steps': 2, 'force_regenerate': False}
    wf, n = {}, 0
    def node(class_type, inputs):
        nonlocal n
        n += 1
        wf[str(n)] = {'class_type': class_type, 'inputs': inputs}
        return str(n)
    def load(name, raw):
        return node('LoadImage', {'image': upload_picture(name, raw)})
    previous = None
    for name, raw in refs:
        inputs = {'image': [load(name, raw), 0]}
        if previous:
            inputs['previous'] = [previous, 0]
        previous = node('FreeVideoReference', inputs)
    if previous:
        gen['references'] = [previous, 0]
    if first:
        gen['first'] = [load(*first), 0]
    if last:
        gen['last'] = [load(*last), 0]
    active = clean_loras(loras)
    if any(item['enabled'] for item in active):
        gen['loras'] = [node('FreeVideoLoRAStack', {'adapters': json.dumps(active)}), 0]
    node('FreeVideoGenerate', gen)
    return wf


def _probe_render_graph():
    """readiness derives the render node classes from the graph build_graph() really submits;
    the throwaway uploader keeps the probe from uploading anything to ComfyUI."""
    return build_graph('readiness probe', 10, 64, 64, 0, [('probe.png', b'x')],
                       ('probe-first.png', b'x'), ('probe-last.png', b'x'), True,
                       [{'name': 'probe.safetensors', 'strength': 1.0, 'enabled': True}],
                       uploader=lambda name, raw: name)


readiness.set_render_graph(_probe_render_graph)


def render_result(prompt_id):
    """None while running; else {'url', 'path', 'run'} of the take, or raises with ComfyUI's error."""
    entry = comfy('/history/' + prompt_id).get(prompt_id)
    if not entry:
        return None
    s = entry.get('status', {})
    for kind, data in s.get('messages', []):
        if kind in ('execution_error', 'execution_interrupted'):
            raise RuntimeError(data.get('exception_message') or kind)
    if not s.get('completed'):
        return None
    for output in entry.get('outputs', {}).values():
        for item in output.get('images', []) + output.get('gifs', []):
            if str(item.get('filename', '')).endswith('.mp4'):
                path = COMFY_OUTPUT / item.get('subfolder', '') / item['filename']
                q = urllib.parse.urlencode({k: item.get(k, '') for k in ('filename', 'subfolder', 'type')})
                return {'url': f'{COMFY}/view?{q}', 'path': str(path), 'run': str(path.parent)}
    raise RuntimeError('render finished but produced no video')


# ---------------------------------------------------------------- ComfyUI websocket (progress)

PROGRESS = {}   # prompt_id -> {'node': str, 'value': int, 'max': int}
FV_PROGRESS = {}   # the running render's overall progress as FreeVideo reports it: fraction, label, remaining, at


def render_progress(job, pid):
    """Update the render stage of `job` from ComfyUI's view of prompt `pid`. FreeVideo's overall fraction (0-1,
    the whole render including the decode) beats ComfyUI's sampler step counter, which reaches its maximum long
    before the render is done, and it keeps flowing for a prompt that was already running when the director
    (re)started. It is broadcast without a prompt id, so it counts only while `pid` is the prompt ComfyUI runs."""
    st = stage(job, 'render')
    fv = dict(FV_PROGRESS)
    if fv.get('at') and time.time() - fv['at'] <= 6:
        q = comfy_queue()
        if q is not None and pid in q[0]:
            st.update(value=max(0, min(100, round(fv['fraction'] * 100))), max=100, unit='%', detail=fv.get('label') or '')
            return
    p = PROGRESS.get(pid, {})
    st.update(value=p.get('value', 0), max=p.get('max', 0))


def ws_target():
    """(host, port) for ComfyUI's /ws, derived from the live COMFY so a swapped ComfyUI
    address (tests, --comfy-url, COMFY_URL) also moves the websocket."""
    sp = urllib.parse.urlsplit(COMFY)
    return sp.hostname, sp.port or (443 if sp.scheme == 'https' else 80)


def ws_listen():
    """Minimal RFC 6455 client: keep PROGRESS updated from ComfyUI's /ws for our client id."""
    while True:
        try:
            host, port = ws_target()
            s = socket.create_connection((host, port), timeout=None)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f'GET /ws?clientId={CLIENT_ID} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n'
                       f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError('handshake closed')
                buf += chunk
            if b' 101 ' not in buf.split(b'\r\n', 1)[0]:
                raise ConnectionError('websocket upgrade refused')
            rest = buf.split(b'\r\n\r\n', 1)[1]
            def read(n):
                nonlocal rest
                while len(rest) < n:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError('closed')
                    rest += chunk
                out, rest = rest[:n], rest[n:]
                return out
            while True:
                b0, b1 = read(2)
                opcode, length = b0 & 0x0F, b1 & 0x7F
                if length == 126:
                    length = struct.unpack('>H', read(2))[0]
                elif length == 127:
                    length = struct.unpack('>Q', read(8))[0]
                payload = read(length)
                if opcode == 8:
                    raise ConnectionError('server closed')
                if opcode == 9:                                   # ping -> masked pong
                    mask = os.urandom(4)
                    s.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask +
                              bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
                    continue
                if opcode != 1:
                    continue                                       # binary previews
                msg = json.loads(payload)
                data = msg.get('data') or {}
                if msg.get('type') == 'freevideo_progress':        # FreeVideo's own whole-render progress: no
                    overall = data.get('overall') or {}            # prompt id, broadcast; applies to the running prompt
                    if isinstance(overall.get('fraction'), (int, float)):
                        FV_PROGRESS.update(fraction=float(overall['fraction']), label=str(data.get('label') or ''),
                                           remaining=overall.get('remaining_seconds'), at=time.time())
                    continue
                pid = data.get('prompt_id')
                if not pid:
                    continue
                state = PROGRESS.setdefault(pid, {'node': None, 'value': 0, 'max': 0})
                if msg.get('type') == 'executing':
                    state.update(node=data.get('node'), value=0, max=0)
                elif msg.get('type') == 'progress':
                    state.update(node=str(data.get('node') or state['node']), value=data.get('value', 0),
                                 max=data.get('max', 0))
        except (OSError, ValueError, ConnectionError):
            time.sleep(2)


# ---------------------------------------------------------------- jobs
# A job reaches a terminal state (`TERMINAL`) whatever its worker does, including raising
# before the first stage activates. Legal moves only -- see TRANSITIONS.

JOB_TIMEOUT = config.value('job_timeout')        # wall-clock cap per job
POLL_SECONDS = config.value('poll_seconds')      # worker poll sleep

TERMINAL = ('finished', 'failed', 'cancelled', 'unknown')
TRANSITIONS = {'queued': {'preparing', 'failed', 'cancelled', 'unknown'},
               'preparing': {'submitted', 'finished', 'failed', 'cancelled', 'unknown'},
               'submitted': {'running', 'finished', 'failed', 'cancelled', 'unknown'},
               'running': {'finished', 'failed', 'cancelled', 'unknown'}}


class Cancelled(Exception):
    pass


class AmbiguousSubmit(Exception):
    """The /prompt POST never got a clean answer; retrying could double-render."""


class JobTimeout(Exception):
    pass


class Busy(Exception):
    """Pre-acceptance refusal: the scene/take is reserved or a prerequisite is missing."""


JOBS = {}
LOCK = threading.Lock()

# One live job may hold each scene (renders, and enhances of its chosen take) and each take
# (enhances). Maps reserve key -> job id; RESERVE_LOCK makes claim-or-refuse atomic.
RESERVES = {}
RESERVE_LOCK = threading.Lock()

SCENE_JOB = {}          # (slug, scene id) -> latest job id, for the film view's `job` field


def scene_key(slug, scene_id):
    return ('scene', slug, str(scene_id))


def take_key(run):
    return ('take', str(run))


def live_owner(job_id):
    job = JOBS.get(job_id)
    return job if job and not job['done'] else None


def unresolved_owner(job_id):
    """An unknown job keeps holding its keys: until ComfyUI shows the prompt stopped, a take
    could still land on that scene or take."""
    job = JOBS.get(job_id)
    return job if job and job['state'] == 'unknown' and job.get('holds') else None


def reserve(job, keys, reuse=False):
    """Claim `keys` for `job` (all-or-nothing). Refuse while another live job holds one:
    return that job when `reuse`, else raise Busy. A job in state unknown always refuses:
    its prompt may still write, and there is no live job to reuse. Replaces the unlocked
    SCENE_JOB write."""
    with RESERVE_LOCK:
        for key in keys:
            holder = RESERVES.get(key)
            owner = live_owner(holder) or unresolved_owner(holder)
            if owner and owner['id'] != job['id']:
                if owner['state'] == 'unknown':
                    ids = ', '.join(owner['prompt_ids']) or 'a ComfyUI prompt'
                    raise Busy(f'busy: job {owner["id"]} state unknown - ComfyUI prompt {ids} may still be executing')
                if reuse:
                    return owner
                what = f'scene {key[2]}' if key[0] == 'scene' else 'this take'
                raise Busy(f'busy: {what} already has a running job')
        for key in keys:
            RESERVES[key] = job['id']
            if key[0] == 'scene':
                SCENE_JOB[(key[1], key[2])] = job['id']
        job['holds'] = list(dict.fromkeys((job.get('holds') or []) + list(keys)))   # never drop earlier holds
    return None


def release(job):
    with RESERVE_LOCK:
        for key in job.get('holds') or []:
            if RESERVES.get(key) == job['id']:
                del RESERVES[key]
        job['holds'] = []


def reserved_scenes(slug):
    with RESERVE_LOCK:
        return {key[2] for key in RESERVES
                if key[0] == 'scene' and key[1] == slug
                and (live_owner(RESERVES[key]) or unresolved_owner(RESERVES[key]))}


def live_jobs(slug=None):
    with LOCK:
        return [j for j in JOBS.values()
                if not j['done'] and (slug is None or j.get('project') == slug)]


def job_backend():
    """host:port of the ComfyUI this process submits to -- what the durable row names."""
    return urllib.parse.urlsplit(COMFY).netloc


def db_kwargs(job):
    """What the durable row needs beyond the live job dict: the backend, and references (paths
    only, never copies) to the project's immutable request/result files for this job."""
    kw = {'backend': job_backend()}
    if job.get('project'):
        try:
            kw['snapshot_path'] = str(projects.job_path(job['project'], job['id']))
            kw['result_path'] = str(projects.result_path(job['project'], job['id']))
        except projects.ProjectError:
            pass
    return kw


def db_note(job):
    """Mirror the live job into the durable store (jobs_db): state, timestamps, prompt ids,
    holds, error, resolved verdict -- the whole row, so the store can never drift from the
    in-memory state machine. A store failure is logged and the job runs on; the ONE place
    that refuses to go on is submit_tracked's pre-submit record_submit (submit step)."""
    try:
        jobs_db.save(job, **db_kwargs(job))
    except Exception as e:                       # the job store must never kill a job or the server
        print(f'jobs: could not record job {job["id"]}: {e}')


def abandon(job):
    """Drop a never-started job (its endpoint refuses the work); releases nothing it held."""
    with LOCK:
        if JOBS.get(job['id']) is job and not job['done']:
            del JOBS[job['id']]
    job['done'] = True
    try:
        jobs_db.forget(job['id'])    # never accepted anywhere: nothing for a restart to reconcile
    except Exception as e:
        print(f'jobs: could not drop abandoned job {job["id"]}: {e}')


def transition(job, to):
    with LOCK:
        if to not in TRANSITIONS.get(job['state'], ()):
            raise AssertionError(f"illegal transition {job['state']} -> {to}")
        job['state'] = to
        if to in ('submitted', 'running') and job.get('started') is None:
            # first moment ComfyUI verifiably holds the work: split "queued for"
            # (waiting in our own queue) from "running for" (execution time)
            job['started'] = time.time()
        if to in TERMINAL:
            job['ended'] = time.time()
        job['done'] = to in TERMINAL
        db_note(job)                 # every state move lands in the store, under the lock


def check_cancel(job):
    if job['cancel']:
        raise Cancelled()


def comfy_queue():
    """(running, pending) prompt ids as ComfyUI sees them right now, or None if it is unreachable."""
    try:
        q = comfy('/queue')
    except (urllib.error.URLError, OSError, ValueError):
        return None
    ids = lambda rows: {row[1] for row in rows or [] if len(row) > 1}
    return ids(q.get('queue_running')), ids(q.get('queue_pending'))


CANCEL_CONFIRM_SECONDS = config.value('cancel_confirm_seconds')   # how long a cancel waits for ComfyUI to drop the prompt


def target_cancel(pids):
    """Stop only this job's prompts: delete ours from the queue, and interrupt only the prompt of ours that is
    executing, naming it ({"prompt_id": ...}, which ComfyUI checks and signals atomically, so the interrupt cannot
    land on a neighbour that starts at that instant). A real interrupt takes seconds to unwind (the sampler only
    looks between steps), so wait, polling, until none of ours is left. Returns True only when ComfyUI shows none
    of ours left within the window; False means we cannot tell."""
    ours = set(pids)
    if not ours:
        return True                                   # nothing was ever submitted
    deadline = time.time() + CANCEL_CONFIRM_SECONDS
    asked = {}                                        # pid -> when the last interrupt for it went out
    while True:
        q = comfy_queue()
        if q is None:
            return False
        running, pending = q
        mine_run, mine_pend = ours & running, ours & pending
        if not mine_run and not mine_pend:
            return True
        now = time.time()
        for pid in sorted(mine_run):                  # asked again only after a pause: an interrupt unwinds slowly
            if now - asked.get(pid, 0) >= 5:
                asked[pid] = now
                try:
                    comfy('/interrupt', json.dumps({'prompt_id': pid}).encode(), {'Content-Type': 'application/json'})
                except (urllib.error.URLError, OSError, ValueError):
                    pass                              # ComfyUI answers /interrupt with an empty body
        if mine_pend:
            try:
                comfy('/queue', json.dumps({'delete': sorted(mine_pend)}).encode(),
                      {'Content-Type': 'application/json'})
            except (urllib.error.URLError, OSError, ValueError):
                pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def confirm_cancel(job):
    """Did ComfyUI really stop this job's prompts? Asked once per job, then remembered."""
    if 'cancel_ok' not in job:
        job['cancel_ok'] = target_cancel(job['prompt_ids'])
    return job['cancel_ok']


def new_job(kind, stage_keys, project=None, scene=None, settings=None):
    s = settings or finishing.defaults()
    job = {'id': uuid.uuid4().hex[:12], 'kind': kind, 'created': time.time(), 'error': None, 'done': False,
           'state': 'queued', 'deadline': time.time() + JOB_TIMEOUT,
           'stages': [{'key': k, 'label': finishing.stage_label(k, s) if k in ('upscale', 'smooth') else STAGES[k],
                       'state': 'pending', 'started': None, 'ended': None,
                       'value': 0, 'max': 0} for k in stage_keys],
           'take': None, 'final': None, 'prompt_ids': [], 'cancel': False, 'finishing': settings,
           'project': project, 'scene': str(scene) if scene is not None else None, 'holds': []}
    with LOCK:
        JOBS[job['id']] = job
    db_note(job)                     # the row exists from the moment a job is accepted
    return job


def stage(job, key):
    return next(s for s in job['stages'] if s['key'] == key)


def activate(job, key):
    """Mark `key` active and every earlier pending/active stage done."""
    now = time.time()
    for s in job['stages']:
        if s['key'] == key:
            if s['state'] != 'active':
                s.update(state='active', started=now)
            return s
        if s['state'] in ('pending', 'active'):
            s.update(state='done', ended=now, started=s['started'] or now)


def finish(job):
    if job['done']:
        return
    now = time.time()
    for s in job['stages']:
        if s['state'] in ('pending', 'active'):
            s.update(state='done', ended=now, started=s['started'] or now)
    try:
        stats.record(job['kind'], now - job['created'])    # never breaks a job
    except Exception:
        pass
    transition(job, 'finished')


def fail(job, error):
    if job['done']:
        return
    for s in job['stages']:
        if s['state'] == 'active':
            s.update(state='error', ended=time.time())
    job['error'] = str(error)
    transition(job, 'failed')


def cancel_job(job):
    if job['done']:
        return
    now = time.time()
    for s in job['stages']:
        if s['state'] in ('pending', 'active'):
            s.update(state='error', ended=now, started=s['started'] or now)
    job['error'] = 'cancelled'
    transition(job, 'cancelled')


def unknown(job, error):
    """We lost track of work ComfyUI may still be running: report it, never resubmit."""
    if job['done']:
        return
    for s in job['stages']:
        if s['state'] == 'active':
            s.update(state='error', ended=time.time())
    job['error'] = str(error)
    transition(job, 'unknown')


def run_job(job, fn, *args, **kwargs):
    """The only way a job thread starts: whatever `fn` does, the job reaches a terminal
    state and its reservations are released -- except for a job left unknown, which keeps
    its holds until reconcile or a user dismissal proves the ComfyUI prompt stopped."""
    try:
        transition(job, 'preparing')
        check_cancel(job)
        fn(job, *args, **kwargs)
        if not job['done']:
            finish(job)
    except Cancelled:
        if confirm_cancel(job):
            cancel_job(job)
        else:                                          # we cannot show the prompts stopped
            ids = ', '.join(job['prompt_ids'])
            unknown(job, f'the cancel of ComfyUI prompt {ids} was not confirmed; it may still be '
                         'executing. The job will not be resubmitted.')
    except (AmbiguousSubmit, JobTimeout) as e:
        ids = ', '.join(job['prompt_ids'])
        if ids:
            unknown(job, f'{e} ComfyUI prompt {ids} may still be executing; the job will not be resubmitted.')
        else:
            unknown(job, f'{e} the submit was never answered; ComfyUI may still be executing. '
                         'The job will not be resubmitted.')
    except Exception as e:                 # a worker blowing up must surface, never kill the server
        fail(job, e)
    finally:
        if not job['done']:                # a terminal state is never skipped, not even here
            with LOCK:
                job['state'], job['done'] = 'failed', True
                job['error'] = job['error'] or 'job ended without a terminal state'
        if job['state'] != 'unknown':                    # unknown: holds survive until it is resolved
            release(job)
        db_note(job)                 # the row catches up with the holds this job ends holding


def poll(job, fetch, on_progress=None):
    """Wait for `fetch()` to return, keeping the cancel and wall-clock guards hot. A run of
    consecutive network failures aborts the job rather than spinning forever."""
    failures = 0
    while True:
        check_cancel(job)
        if time.time() > job['deadline']:
            raise JobTimeout(f'no result within {int(job["deadline"] - job["created"])}s;')
        try:
            got = fetch()
            failures = 0
        except (OSError, ValueError):
            failures += 1
            if failures > 3:
                raise
            got = None
        if got is not None:
            return got
        if on_progress:
            on_progress()
        time.sleep(POLL_SECONDS)


def do_render(job, graph, finish_after, on_take=None, on_final=None):
    """Render one take from its prebuilt graph; when `finish_after`, chain the enhance stages into the
    same job. `graph` is the exact workflow the job's snapshot recorded before submission."""
    st = activate(job, 'render')
    check_cancel(job)                                  # a cancel after prep never submits
    pid = submit_tracked(job, {'prompt': graph})
    check_cancel(job)                                  # a cancel that landed mid-submit still counts
    transition(job, 'submitted')
    transition(job, 'running')

    def progress():
        render_progress(job, pid)

    take = poll(job, lambda: render_result(pid), progress)
    job['take'] = take
    if on_take:
        on_take(take)
    if finish_after:
        enhance_stages(job, take['run'], on_final)


def do_render_adhoc(job, args, finish_after):
    """Ad-hoc render from the prompt page: no project, hence no generation record; build, then run."""
    do_render(job, build_graph(**args), finish_after)


def enhance_stages(job, run, on_final=None):
    """The finishing stages `job` was built for -- upscaler and frame smoother only when the
    project's finishing settings ask for them -- run for `run`; `job` must already hold what
    it may touch."""
    activate(job, next(s['key'] for s in job['stages'] if s['key'] != 'render'))
    check_cancel(job)                                  # a cancel after prep never submits
    run_dir = enhance.run_dir_of(run)
    workdir = enhance.new_workdir(run_dir, job['id'])   # private to this job; two enhances never share a name
    job['workdir'] = str(workdir)
    db_note(job)                     # the kept temp dir belongs in the durable row before anything can go unknown
    keep = False                       # an unknown job keeps the dir: its prompt may still read it
    try:
        source, _ = enhance.intermediate(run_dir, workdir)
        enhance.free_gpu()
        pid = submit_tracked(job, {'prompt': enhance.graph(source, run_dir.name,
                                                           settings=job.get('finishing'))})
        check_cancel(job)                              # a cancel that landed mid-submit still counts
        if job['state'] == 'preparing':                # enhance-only job; a render chain is already running
            transition(job, 'submitted')
            transition(job, 'running')
        if job['take'] is None:
            job['take'] = {'path': str(run_dir / 'video.mp4'), 'run': str(run_dir), 'url': None}

        def progress():
            p = PROGRESS.get(pid, {})
            nodes = enhance.node_stages(job.get('finishing'))
            if p.get('node') in nodes:
                st = activate(job, nodes[p['node']])
                st.update(value=p.get('value', 0), max=p.get('max', 0))

        final = poll(job, lambda: enhance.result(pid), progress)
    except Cancelled:
        keep = not confirm_cancel(job)                 # unconfirmed: ComfyUI may still be working
        raise
    except (AmbiguousSubmit, JobTimeout):
        keep = True                                    # the job turns unknown upstream
        raise
    finally:
        if not keep:
            enhance.remove_workdir(workdir)   # the lossless raw mkv is only needed while ComfyUI reads it
            job['workdir'] = None
            db_note(job)             # the temp dir is gone: the durable row must say so too
        # kept dirs are deleted by release_resolved once reconcile or a dismissal proves the stop
    job['final'] = final
    if on_final:
        on_final(final)


def job_finishing(settings=None):
    """The finishing record an enhance runs with. The flat legacy keys (resolution/multiplier/
    crf/fps plus the config-level model file names) stay for every pre-existing reader;
    `settings` is the canonical per-project switch set, `stages` the web stage keys actually
    run, and `output_fps` the rate the finished file will carry. Every record that triggers an
    enhance snapshots these; the enhance graph itself is ephemeral (temp file paths) and
    rebuilds from them plus the take."""
    s = settings or finishing.defaults()
    return dict(enhance.DEFAULTS, fps=enhance.FPS,
                resolution=s['upscale']['height'],
                multiplier=s['interpolation']['factor'] if s['interpolation']['enabled'] else 1,
                crf=s['encode']['crf'],
                settings=s, stages=finishing.enabled_stages(s),
                output_fps=finishing.output_fps(enhance.FPS, s))


def write_enhance_snapshot(job, run):
    """Record an enhance request for a project take BEFORE submitting it: the finishing settings plus
    what was known about the source take. Ad-hoc enhances have no project and are not recorded."""
    slug = job['project']
    p = projects.load(slug)
    scene_id, take = projects.take_for_run(p, run)
    projects.write_job_snapshot(slug, {
        'id': job['id'], 'kind': 'enhance', 'created': job['created'], 'project': slug,
        'scene': job.get('scene') or scene_id,
        'source': {'take': take and take.get('id'), 'run': run,
                   'provenance': projects.take_provenance(take) if take else 'unknown'},
        'finishing': job_finishing(job.get('finishing')), 'instructions': projects.instructions_of(p),
        **({'retry_of': job['retry_of']} if job.get('retry_of') else {})})


def write_job_result(job, take_id=None):
    """The single result write beside a job's snapshot: what the job produced and when. The snapshot
    is never edited; this sibling file is written exactly once, after the job finishes."""
    projects.write_job_result(job['project'], job['id'], {
        'job': job['id'], 'take_id': take_id,
        'run': job['take'] and job['take'].get('run'),
        'path': job['take'] and job['take'].get('path'),
        'url': job['take'] and job['take'].get('url'),
        'final': job['final'] and job['final'].get('path'),
        'final_url': job['final'] and job['final'].get('url'),
        'prompt_ids': list(job['prompt_ids']),
        'started': job['created'], 'finished': time.time(),
        'stages': [{'key': s['key'], 'started': s['started'], 'ended': s['ended']} for s in job['stages']]})


def enhance_late_record(job, on_final):
    """What a finished enhance must record whenever the proof arrives -- live, or late after
    the job went unknown: the final and the project result file. Every write is replay-safe."""
    def late(verdict):                                   # an unknown enhance proved to have finished
        if verdict != 'finished' or not job['prompt_ids']:
            return
        final = enhance.result(job['prompt_ids'][-1])
        job['final'] = final
        if on_final:
            on_final(final)
        if job.get('project'):
            try:
                write_job_result(job)                    # written once; a clash means it already was
            except projects.ProjectError:
                pass
    return late


def enhance_job(job, run, on_final=None, keys=()):
    """Thread body for a standalone enhance: hold the take (plus the endpoint's scene key), record the
    finishing request against the project's take, then finish it. An ad-hoc enhance records nothing."""
    reserve(job, [take_key(run), *keys])
    if job.get('project'):
        write_enhance_snapshot(job, run)
    job['late_record'] = enhance_late_record(job, on_final)
    enhance_stages(job, run, on_final)
    if job.get('project'):
        write_job_result(job)


# ---------------------------------------------------------------- resolving unknown jobs

RECONCILE_SECONDS = config.value('reconcile_seconds')   # background ask-again interval


def comfy_history_detail(pid):
    """(outcome, message) as ComfyUI's history answers for one prompt: 'completed', 'interrupted'
    or 'failed' (with ComfyUI's own exception text when it left one), 'lost' when it left no
    trace, or (None, None) when ComfyUI could not be reached to ask."""
    try:
        h = comfy(f'/history/{pid}')
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    entry = h.get(pid)
    if entry is None:
        return 'lost', None
    status = entry.get('status') or {}
    message, kinds = None, set()
    for kind, data in status.get('messages') or []:
        kinds.add(kind)
        if kind in ('execution_error', 'execution_interrupted'):
            message = data.get('exception_message') or kind
    # Real ComfyUI nests status_str inside `status` and sets it to 'error' for BOTH a runtime failure
    # and a user interrupt, so the message kinds tell them apart (a failed run also never completes).
    if 'execution_error' in kinds:
        return 'failed', message
    if 'execution_interrupted' in kinds:
        return 'interrupted', message
    if status.get('status_str') == 'error' or entry.get('status_str') == 'error' or status.get('failed'):
        return 'failed', message
    if status.get('completed'):
        return 'completed', None
    return 'interrupted', message


def comfy_history_outcome(pid):
    """What ComfyUI's history says about one prompt: 'completed', 'interrupted' or 'failed',
    'lost' when it left no trace, or None when ComfyUI could not be reached to ask."""
    return comfy_history_detail(pid)[0]


def release_resolved(job, verdict, by='reconcile'):
    """The unknown job's prompt provably stopped (or the user called it): let the worker record a
    late result, tidy any kept temp dir, remember what we learned, and free the reservations."""
    late = job.get('late_record')
    if late:
        try:
            late(verdict)
        except Exception as e:
            job['late_record_error'] = str(e)            # a failed late record must not strand holds
    wd = job.get('workdir')
    if wd:
        enhance.remove_workdir(Path(wd))
        job['workdir'] = None
    job['resolved'] = {'by': by, 'verdict': verdict, 'at': time.time()}
    release(job)
    db_note(job)                     # the resolved verdict and freed holds are durable


def reconcile(job):
    """Ask ComfyUI what became of an unknown job's prompts. 'running' while one is still queued;
    None while nobody can tell; a proven verdict releases the holds and records the late result.
    Idempotent: once resolved the stored verdict is simply returned."""
    if job.get('resolved'):
        return job['resolved']['verdict']
    if not job['prompt_ids']:
        return None                                      # the submit was never answered: nothing to ask
    q = comfy_queue()
    if q is None:
        return None                                      # ComfyUI unreachable: keep holding
    if q[0] & set(job['prompt_ids']) or q[1] & set(job['prompt_ids']):
        return 'running'
    outcomes = [comfy_history_outcome(pid) for pid in job['prompt_ids']]
    if any(o is None for o in outcomes):
        return None
    if all(o == 'completed' for o in outcomes):
        verdict = 'finished'
    elif any(o == 'interrupted' for o in outcomes):
        verdict = 'interrupted'
    elif any(o == 'failed' for o in outcomes):
        verdict = 'failed'
    else:
        verdict = 'lost'                                 # neither queued nor in history: never ran
    release_resolved(job, verdict)
    return verdict


def unresolved_jobs():
    with LOCK:
        return [j for j in JOBS.values()
                if j['state'] == 'unknown' and j.get('holds') and not j.get('resolved')]


def reconcile_loop():
    """Ask ComfyUI now and then what happened to unresolved unknown jobs: on proof the late
    result gets recorded and the scene or take is freed for new work."""
    while True:
        time.sleep(RECONCILE_SECONDS)
        for job in unresolved_jobs():
            try:
                reconcile(job)
            except Exception:
                pass                                     # still holding; the next round asks again
        try:
            if jobs_db.list_jobs(states=RECOVER_STATES, limit=1):
                recover_jobs()        # rows still open in the store: ask ComfyUI again
        except Exception:
            pass                                       # the next round asks again
        try:
            sweep_enhance_tmp()
        except Exception:
            pass                                       # the next round sweeps again


def sweep_enhance_tmp():
    """Crash leftovers must not pile up in ComfyUI's input folder: drop enhance temp dirs that
    no live or unresolved job owns any more. Runs at startup and between reconcile rounds."""
    keep = {Path(job['workdir']).name for job in JOBS.values() if job.get('workdir')}
    return enhance.sweep_workdirs(keep)



# ------------------------------------------------------------------ startup recovery
# A restart must neither lose unfinished work nor duplicate it. `recover_jobs` reads the durable
# store and asks ComfyUI what became of every row that is not terminal yet, plus every unresolved
# 'unknown' row: still queued or running -> the job is rebuilt in memory and its EXISTING prompt
# polled (never resubmitted); provably completed -> take/final and the result file are recorded
# through the SAME recorders a live job uses (all replay-safe); failed or interrupted -> the
# matching terminal state carrying ComfyUI's own message; gone from queue and history ->
# 'unknown', still holding its scene, with `retry_allowed` only where the submit provably never
# reached ComfyUI. When ComfyUI itself is unreachable nothing moves: rows keep their state, get a
# `recovering_at` marker, and the reconcile loop runs the pass again.

RECOVER_STATES = ('submitting', 'queued', 'preparing', 'submitted', 'running')
NOT_REACHABLE = 'ComfyUI not reachable yet'


class ComfyWentAway(Exception):
    """ComfyUI stopped answering mid-pass: leave the row exactly as it was and try again later."""


def _rebuilt_job(row):
    """The live job dict a durable row rebuilds: identity, prompt ids and flags come from the
    row; the state machine restarts at 'queued' (run_job and every settle path move legally
    from there) and the wall-clock cap restarts from now. The finishing record inside the
    job's own snapshot decides which stages it was scheduled with -- a resumed job shows the
    stages it actually ran, not whatever the project's settings say today."""
    settings = None
    if row['project']:
        try:
            snap, _ = projects.read_job(row['project'], row['id'])
        except projects.ProjectError:
            snap = None
        rec = (snap or {}).get('finishing')
        settings = finishing.settings_of_snapshot(rec) if rec else None
    try:
        keys = scene_stage_keys(row['kind'], settings)
    except KeyError:
        keys = ['render']
    s = settings or finishing.defaults()
    return {'id': row['id'], 'kind': row['kind'], 'created': row['created'] or time.time(),
            'started': row['started'], 'ended': None, 'error': None, 'done': False,
            'state': 'queued', 'deadline': time.time() + JOB_TIMEOUT,
            'stages': [{'key': k, 'label': finishing.stage_label(k, s) if k in ('upscale', 'smooth') else STAGES[k],
                        'state': 'pending', 'started': None,
                        'ended': None, 'value': 0, 'max': 0} for k in keys],
            'take': ({'path': str(Path(row['run']) / 'video.mp4'), 'run': row['run'], 'url': None}
                     if row.get('run') else None),
            'final': None, 'prompt_ids': list(row['prompt_ids'] or []), 'cancel': False,
            'finishing': settings,
            'project': row['project'], 'scene': row['scene'], 'holds': [],
            'correlation': row['correlation'], 'workdir': row['workdir'],
            'take_id': row['take_id'], 'retry_allowed': bool(row['retry_allowed']),
            'retry_of': row['retry_of'], 'retried_as': row['retried_as'],
            'recovering_at': None}       # the store's marker is not copied onto the live job


def recovery_snapshot(job):
    """The immutable request record the job wrote before submitting (preset, seed, variant,
    continuation, and for an enhance the source run), or None when there is nothing to read."""
    if not job.get('project'):
        return None
    try:
        snapshot, _ = projects.read_job(job['project'], job['id'])
    except projects.ProjectError:
        return None
    return snapshot


def _resume_render(job, slug, scene_id, preset_name, take_id, seed, variant, source_take,
                   render_pid, enhance_pid):
    """Thread body for a render a restart found inside ComfyUI: no build, no submit, no new GPU
    work -- poll the existing prompt and record through the same take/final recorders and result
    file a fresh render uses. A Final render whose finishing stages were never submitted ends
    render-only, saying so: starting the enhance now would be an automatic resubmit, which
    recovery NEVER does."""
    activate(job, 'render')
    check_cancel(job)
    transition(job, 'submitted')
    transition(job, 'running')

    def progress():
        render_progress(job, render_pid)

    try:
        take = poll(job, lambda: render_result(render_pid), progress)
    except (RuntimeError, KeyError):
        take = job.get('take')          # history purged but the run dir is known from the row
        if take is None:
            raise
    job['take'] = take
    if slug and scene_id:
        take_recorder(slug, scene_id, job['id'], take_id, preset_name,
                      seed, variant, source_take)(take)
        if enhance_pid:
            on_final = final_recorder(slug, scene_id, lambda: take['run'], job['id'])
            activate(job, next(s['key'] for s in job['stages'] if s['key'] != 'render'))
            check_cancel(job)

            def eprogress():
                p = PROGRESS.get(enhance_pid, {})
                if p.get('node') in (nodes := enhance.node_stages(job.get('finishing'))):
                    st = activate(job, nodes[p['node']])
                    st.update(value=p.get('value', 0), max=p.get('max', 0))

            final = poll(job, lambda: enhance.result(enhance_pid), eprogress)
            job['final'] = final
            on_final(final)
        elif preset_name == 'final':
            job['error'] = ('the render completed; its finishing stages had not been submitted when '
                            'the director stopped, so the take is recorded unenhanced. Run an Enhance '
                            'for this take; no new GPU work was started automatically.')
    if slug:
        try:
            write_job_result(job, take_id)     # written once; a clash means it already was
        except projects.ProjectError:
            pass
    if job.get('workdir'):
        enhance.remove_workdir(Path(job['workdir']))   # the raw mkv is only needed while ComfyUI reads it
        job['workdir'] = None


def _resume_enhance(job, run, on_final):
    """Thread body for a standalone enhance a restart found inside ComfyUI: poll its existing
    prompt only; record the final and the result file exactly as a fresh enhance does."""
    pid = job['prompt_ids'][-1]
    activate(job, job['stages'][0]['key'])
    check_cancel(job)
    transition(job, 'submitted')
    transition(job, 'running')

    def progress():
        p = PROGRESS.get(pid, {})
        nodes = enhance.node_stages(job.get('finishing'))
        if p.get('node') in nodes:
            st = activate(job, nodes[p['node']])
            st.update(value=p.get('value', 0), max=p.get('max', 0))

    final = poll(job, lambda: enhance.result(pid), progress)
    job['final'] = final
    if on_final:
        on_final(final)
    if job.get('workdir'):
        enhance.remove_workdir(Path(job['workdir']))   # the raw mkv is only needed while ComfyUI reads it
        job['workdir'] = None
    if job.get('project'):
        try:
            write_job_result(job, job.get('take_id'))
        except projects.ProjectError:
            pass


def _lost_message(job, row):
    """What 'gone from queue and history' means, by evidence class -- and whether a Retry could
    honestly be offered. A row still 'submitting' proves only that the POST left the director
    unanswered: the request may never have reached ComfyUI, so a Retry is honest. A submit
    ComfyUI had acknowledged (the row moved past 'submitting') may simply have been purged, and
    resubmitting on that guess could double the GPU work."""
    if row['state'] == jobs_db.SUBMITTING:
        job['retry_allowed'] = True
        return ('the submit was not confirmed: ComfyUI has no record of its prompt, so the request '
                'may never have reached it. Nothing was resubmitted; Retry is available.')
    return ('ComfyUI has no record of this job in its queue or history: the work may have completed '
            'and been purged, or been lost. Reconcile or dismiss it; it is never resubmitted '
            'automatically.')


def _chain_verdict(job, out, msg, enhance_side=False):
    """A non-completed settle verdict, told with ComfyUI's own message when it left one.
    `enhance_side`: the render landed and its take is already recorded -- only the finishing
    prompt vanished, so the scene keeps its reservation until reconciled or dismissed."""
    if out == 'failed':
        if job.get('workdir'):
            enhance.remove_workdir(Path(job['workdir']))   # a kept temp dir dies with the verdict
            job['workdir'] = None
        fail(job, msg or 'ComfyUI reported an error')
        release(job)
        db_note(job)
        return 'failed'
    if out == 'interrupted':
        if job.get('workdir'):
            enhance.remove_workdir(Path(job['workdir']))
            job['workdir'] = None
        cancel_job(job)
        job['error'] = msg or 'interrupted by ComfyUI'
        release(job)
        db_note(job)
        return 'cancelled'
    if enhance_side:
        unknown(job, 'the render completed and its take is recorded, but ComfyUI has no record of '
                     'the finishing prompt either; the scene keeps its reservation until the job is '
                     'reconciled or dismissed. Nothing was resubmitted.')
    else:
        unknown(job, msg)
    db_note(job)
    return 'unknown'


def prep_late_record(job, snap):
    """Identity and late-proof plumbing every non-live render/enhance needs: the take_id it
    will record and the callback that records a proven finish whenever the proof arrives --
    live, from a recovery settle, or from a reconcile triggered by the retry route. Shared by
    _settle and the retry route's display-only rebuild so the two can never drift."""
    if job['kind'].startswith('render'):
        preset_name = 'final' if job['kind'] == 'render-final' else 'draft'
        finish_after = PRESETS[preset_name]['finish']
        take_id = job['take_id'] or (snap or {}).get('take_id') or projects.new_take_id()
        job['take_id'] = take_id
        slug, scene_id = job['project'], job['scene']
        on_take = (take_recorder(slug, scene_id, job['id'], take_id, preset_name,
                                 (snap or {}).get('seed'), (snap or {}).get('variant') or None,
                                 source_take_of(snap) if snap else None)
                   if slug and scene_id else None)
        on_final = (final_recorder(slug, scene_id, lambda: job['take']['run'], job['id'])
                    if slug and scene_id else None)
        job['late_record'] = render_late_record(job, on_take, on_final, take_id, finish_after)
        return {'render': True, 'take_id': take_id, 'finish_after': finish_after,
                'on_take': on_take, 'on_final': on_final}
    run = (job['take'] or {}).get('run') or ((snap or {}).get('source') or {}).get('run')
    on_final = (final_recorder(job['project'], job['scene'], lambda: run, job['id'])
                if job['project'] and job['scene'] and run else None)
    job['late_record'] = enhance_late_record(job, on_final)
    return {'render': False, 'run': run, 'on_final': on_final}


def _settle(job, row, snap, details):
    """ComfyUI's queue holds nothing of this job any more: its history gives the verdict, and
    the job moves to the terminal state -- and makes exactly the records -- the live worker
    would have. Returns a summary key."""
    pids, kind, slug, scene_id = job['prompt_ids'], job['kind'], job['project'], job['scene']
    if any(o is None for o, _ in details):
        raise ComfyWentAway()
    render = kind.startswith('render')
    prep = prep_late_record(job, snap)
    if render:
        take_id, finish_after = prep['take_id'], prep['finish_after']
        on_take, on_final = prep['on_take'], prep['on_final']
    else:
        run, on_final = prep['run'], prep['on_final']
        take_id = job.get('take_id')

    def finish_recorded():
        job['state'] = 'preparing'        # legal stand-in: finish() cannot close a 'queued' job
        finish(job)
        if slug:
            try:
                write_job_result(job, take_id)
            except projects.ProjectError:
                pass                      # the result file exists: this pass is a replay
        if job.get('workdir'):                         # the raw mkv is only needed while ComfyUI reads it
            enhance.remove_workdir(Path(job['workdir']))
            job['workdir'] = None
        release(job)
        db_note(job)
        return 'recorded'

    if not pids:
        # record_submit writes the row BEFORE the POST leaves, so a row that never grew prompt
        # ids provably never reached ComfyUI: nothing was lost, and a Retry would be honest.
        job['retry_allowed'] = True       # set BEFORE unknown()'s write so it is persisted
        unknown(job, 'the job was interrupted before anything reached ComfyUI: nothing had been '
                     'submitted, so nothing was resubmitted.')
        release(job)                      # provably nothing runs: the scene must not stay blocked
        db_note(job)                      # the freed holds are durable too
        return 'unknown'
    out, msg = (details[0] if render else details[-1])
    if out == 'completed':
        try:
            got = render_result(pids[0]) if render else enhance.result(pids[-1])
        except (RuntimeError, KeyError) as e:
            fail(job, f'ComfyUI says the work completed but left no video: {e}')
            release(job)
            return 'failed'
        if render:
            job['take'] = got
            if on_take:
                on_take(got)
            if finish_after and len(pids) > 1:
                e_out, e_msg = details[-1]
                if e_out != 'completed':
                    return _chain_verdict(job, e_out, e_msg, enhance_side=True)
                final = enhance.result(pids[-1])
                job['final'] = final
                if on_final:
                    on_final(final)
            elif finish_after:
                job['error'] = ('the render completed; its finishing stages had not been submitted '
                                'when the director stopped, so the take is recorded unenhanced. Run '
                                'an Enhance for this take; no new GPU work was started automatically.')
        else:
            job['final'] = got
            if on_final:
                on_final(got)
        return finish_recorded()
    if out == 'lost':
        return _chain_verdict(job, out, _lost_message(job, row))
    return _chain_verdict(job, out, msg)


def _reattach(job, row, snap):
    """ComfyUI verifiably still holds one of this job's prompts: rebuild it in memory, reclaim
    its reservations and poll the EXISTING prompt -- the only new work recovery ever starts is
    watching."""
    if job['kind'].startswith('render'):
        preset_name = 'final' if job['kind'] == 'render-final' else 'draft'
        take_id = job['take_id'] or (snap or {}).get('take_id') or projects.new_take_id()
        job['take_id'] = take_id
        pids = job['prompt_ids']
        body = _resume_render
        args = (job['project'], job['scene'], preset_name, take_id,
                (snap or {}).get('seed'), (snap or {}).get('variant') or None,
                source_take_of(snap) if snap else None,
                pids[0], pids[-1] if len(pids) > 1 else None)
    else:
        run = (job['take'] or {}).get('run') or ((snap or {}).get('source') or {}).get('run')
        if run and job['take'] is None:
            job['take'] = {'path': str(Path(run) / 'video.mp4'), 'run': run, 'url': None}
        on_final = (final_recorder(job['project'], job['scene'], lambda: run, job['id'])
                    if job['project'] and job['scene'] and run else None)
        body, args = _resume_enhance, (run, on_final)
    with LOCK:
        JOBS[job['id']] = job
    keys = [tuple(k) for k in (row['holds'] or [])]
    try:
        if keys:
            reserve(job, keys)
    except Busy as e:
        unknown(job, f'{e}: this job\'s ComfyUI prompt may still be executing; nothing was resubmitted.')
        return 'unknown'
    threading.Thread(target=run_job, args=(job, body, *args), daemon=True).start()
    return 'reattached'


def _recover_one(row, queue_ids):
    """Recover one durable row against a fresh ComfyUI queue snapshot."""
    job = _rebuilt_job(row)
    try:
        jobs_db.clear_recovering(job['id'])            # ComfyUI reached: handled again right now
    except jobs_db.JobDbError:
        pass

    snap = recovery_snapshot(job)
    if job['prompt_ids'] and (set(job['prompt_ids']) & queue_ids):
        return _reattach(job, row, snap)
    with LOCK:
        JOBS[job['id']] = job             # on the board while the verdict is being established
    keys = [tuple(k) for k in (row['holds'] or [])]
    try:
        if keys:
            reserve(job, keys)
    except Busy as e:
        unknown(job, f'{e}: this job\'s ComfyUI prompt may still be executing; nothing was resubmitted.')
        return 'unknown'
    details = [comfy_history_detail(pid) for pid in job['prompt_ids']]
    try:
        return _settle(job, row, snap, details)
    except ComfyWentAway:
        with LOCK:
            if JOBS.get(job['id']) is job:
                del JOBS[job['id']]       # nothing was learned: drop the stand-in job
        release(job)
        jobs_db.mark_recovering(job['id'])
        return 'recovering'


def recover_jobs(comfy=None):
    """Startup (and periodic) reconciliation against ComfyUI -- see the notes at the top of
    this section. Pass `comfy` to aim one pass at a specific server (tests); threads the pass
    starts use the module globals, so the swap covers those too."""
    global COMFY
    saved = COMFY
    if comfy:
        COMFY = enhance.COMFY = comfy
    try:
        return _recovery_pass()
    finally:
        COMFY = saved


def _recovery_pass():
    summary = {'recovering': 0, 'reattached': 0, 'recorded': 0, 'failed': 0,
               'unknown': 0, 'skipped': 0}
    try:
        rows = jobs_db.list_jobs(states=RECOVER_STATES)
        rows += [r for r in jobs_db.list_jobs(states=('unknown',)) if not r['resolved']]
    except jobs_db.JobDbError as e:
        print(f'jobs: recovery cannot read the store: {e}')
        return summary
    if not rows:
        return summary
    q = comfy_queue()
    if q is None:
        for row in rows:                 # ComfyUI is not up (yet): change nothing, mark waiting
            if row['id'] not in JOBS:
                try:
                    jobs_db.mark_recovering(row['id'])
                    summary['recovering'] += 1
                except jobs_db.JobDbError:
                    pass
        return summary
    queue_ids = q[0] | q[1]
    for row in rows:
        if row['id'] in JOBS:            # a live or board-present job: the pass must not touch it
            try:
                jobs_db.clear_recovering(row['id'])
            except jobs_db.JobDbError:
                pass
            summary['skipped'] += 1
            continue
        try:
            summary[_recover_one(row, queue_ids)] += 1
        except Exception as e:
            print(f'jobs: could not recover {row["id"]}: {e}')
    return summary


def loadable_job(job_id):
    """Routes keep answering persisted jobs after a restart: live and settled jobs are in
    memory; an open store row nothing has loaded yet is loaded -- and reconciled against
    ComfyUI -- on demand, so Reconcile works on a job recovery has not reached. Terminal rows
    stay display-only; while ComfyUI is unreachable an open row answers nothing HERE, but the
    retry route has its own door (_retryable_row) and answers the honest 409 instead."""
    job = JOBS.get(job_id)
    if job:
        return job
    try:
        row = jobs_db.get(job_id)
    except jobs_db.JobDbError:
        return None
    if not row or row['state'] in TERMINAL or row['resolved']:
        return None
    q = comfy_queue()
    if q is None:
        return None
    _recover_one(row, q[0] | q[1])
    return JOBS.get(job_id)


# ---------------------------------------------------------------- explicit retry
# The other door for an unknown job: when the submit provably never reached ComfyUI
# (retry_allowed), the user may ask for the RECORDED request to be submitted again -- once.
# The new job is linked through the store (retry_of / retried_as), a second click finds the
# first answer (find_retry), and the route re-checks ComfyUI first: a prompt that in the
# meantime turned up running or finished is adopted, never duplicated. Nothing here ever
# calls itself: the /api/job/<id>/retry route is retry_job()'s only caller.

RETRY_LOCK = threading.Lock()         # two simultaneous clicks must not create two replays


def retry_warning(job, verdict):
    """What the retry tells the user it is doing, from the same evidence that made Retry
    honest: a prompt id ComfyUI was never seen to acknowledge, or a submit never answered."""
    if verdict == 'failed':
        return ('ComfyUI reported the original prompt failed; its recorded request is '
                'submitted again as a new job.')
    if verdict == 'interrupted':
        return ('ComfyUI shows the original prompt was interrupted; its recorded request is '
                'submitted again as a new job.')
    if not job['prompt_ids']:
        return ('the submit was never answered: the request may never have reached ComfyUI, or '
                'ComfyUI may still be executing it. The recorded request is submitted again as '
                'a new job -- if the original did reach ComfyUI this may generate a second take.')
    ids = ', '.join(job['prompt_ids'])
    if job.get('correlation') == 'client-supplied':
        return (f'ComfyUI has no record of prompt {ids}: it may never have reached ComfyUI. The '
                'recorded request is submitted again as a new job; this may generate a second take.')
    return (f'ComfyUI has no record of prompt {ids} although the submit left this machine '
            'awaiting an answer: the work may never have arrived. The recorded request is '
            'submitted again as a new job; this may generate a second take.')


def recorded_inputs(job):
    """What the recorded request pins down for a replay: (args, run, snap) rebuilt from the
    job's own request record -- prompt, seed and content-addressed pictures the original
    submit was built from, which the scene may since have changed. Busy whenever a piece is
    gone: a retry must never quietly substitute today's settings for the recorded ones."""
    slug, snap = job.get('project'), recovery_snapshot(job)
    if job['kind'] == 'enhance':
        run = ((snap or {}).get('source') or {}).get('run') or (job.get('take') or {}).get('run')
        if not run:
            raise Busy('the recorded inputs are no longer available: this enhance names no source take')
        try:
            enhance.run_dir_of(run)
        except FileNotFoundError:
            raise Busy(f'the recorded inputs are no longer available: the source take {run} is no '
                       'longer in the output folder')
        return None, run, snap
    if not snap:
        raise Busy('the recorded inputs are no longer available: this job left no request record to replay')
    refs = []
    for r in sorted(snap.get('refs') or [], key=lambda r: r.get('index') or 0):
        try:
            raw = projects.path(slug, r['store']).read_bytes()
        except OSError:
            raise Busy(f'the recorded inputs are no longer available: reference picture {r["name"]} is gone')
        refs.append((r['name'], raw))
    first = None
    if snap.get('first'):
        f = snap['first']
        try:
            raw = projects.path(slug, f['store']).read_bytes()
        except OSError:
            raise Busy(f'the recorded inputs are no longer available: the recorded first frame {f["name"]} is gone')
        first = (f['name'], raw)
    args = dict(prompt=snap['prompt'], seconds=snap['seconds'], width=snap['width'],
                height=snap['height'], seed=snap['seed'], refs=refs, first=first, last=None,
                two_pass=snap['two_pass'], loras=snap.get('loras') or [])
    return args, None, snap


def retry_render_job(job, slug, scene_id, snap, take_id, args):
    """Thread body of a retried render: rebuild the graph from the RECORDED request (not from
    whatever the scene says now), write this job's own snapshot -- carrying retry_of -- then
    run exactly like a fresh render: reserve, submit, record through the same recorders."""
    reserve(job, [scene_key(slug, scene_id)])
    graph = build_graph(**args)
    preset_name = snap['preset']
    snapshot = render_snapshot(job, slug, scene_id, take_id, preset_name, snap['variant'],
                               args, graph, (snap.get('director') or {}).get('model'),
                               (snap.get('first') or {}).get('source'))
    snapshot['retry_of'] = job['retry_of']
    projects.write_job_snapshot(slug, snapshot)
    on_take = take_recorder(slug, scene_id, job['id'], take_id, preset_name, snap['seed'],
                            snap['variant'], source_take_of(snapshot))
    on_final = final_recorder(slug, scene_id, lambda: job['take']['run'], job['id'])
    job['late_record'] = render_late_record(job, on_take, on_final, take_id, PRESETS[preset_name]['finish'])
    do_render(job, graph, PRESETS[preset_name]['finish'], on_take, on_final)
    write_job_result(job, take_id)


def retry_job(job):
    """The user's explicit Retry of an unknown job (the route proved state unknown, unresolved,
    retry_allowed). ComfyUI is re-checked first: a prompt that turned up running is adopted
    (_recover_one reattaches it); one that proved finished already recorded its take through
    reconcile -- either way NOTHING is submitted and the caller gets a Busy. Otherwise the
    recorded request goes out as a NEW job linked through the store, the old row resolves
    'retried' by 'user', and a second click finds the first answer (find_retry). RETRY_LOCK
    keeps two simultaneous clicks from racing past that check."""
    with RETRY_LOCK:
        try:
            prior = jobs_db.find_retry(job['id'])
        except jobs_db.JobDbError as e:
            raise Busy(f'the job store cannot be read: {e}')      # nothing submitted
        if prior:
            live = JOBS.get(prior['id'])
            return {'retry_of': job['id'], 'already': True,
                    'job': public(live) if live else
                    {'id': prior['id'], 'kind': prior['kind'], 'state': prior['state'],
                     'project': prior['project'], 'scene': prior['scene']}}
        q = comfy_queue()
        if q is None:
            raise Busy(f'ComfyUI is not answering at {COMFY}: a retry cannot first check whether '
                       'the original submit survived; try again once ComfyUI answers')
        verdict = reconcile(job) if job['prompt_ids'] else None
        if verdict == 'running':
            old_id = job['id']
            release(job)                       # _recover_one re-reserves from the durable row
            try:
                row = jobs_db.get(old_id)
            except jobs_db.JobDbError:
                row = None
            if row and not row['resolved']:
                _recover_one(row, q[0] | q[1])
            raise Busy('the original job turned up in ComfyUI: it is reattached to its existing '
                       'prompt and nothing was submitted')
        if verdict == 'finished':
            raise Busy('the original job completed in ComfyUI and its take was recorded from the '
                       'prompt; nothing was submitted')
        args, run, snap = recorded_inputs(job)
        st = finishing.settings_of_snapshot((snap or {}).get('finishing'))
        new = new_job(job['kind'], scene_stage_keys(job['kind'], st),
                      project=job.get('project'), scene=job.get('scene'), settings=st)
        new['retry_of'] = job['id']
        if job['kind'] != 'enhance':
            new['take_id'] = projects.new_take_id()
        db_note(new)                           # the retry link is durable before any thread starts
        job['retried_as'] = new['id']
        release_resolved(job, 'retried', by='user')   # frees the holds the replay re-reserves
        if job['kind'] == 'enhance':
            slug, scene_id = job.get('project'), job.get('scene')
            keys = [scene_key(slug, scene_id)] if slug and scene_id else []
            on_final = final_recorder(slug, scene_id, lambda: run, new['id']) if slug and scene_id else None
            threading.Thread(target=run_job, args=(new, enhance_job, run, on_final, keys),
                             daemon=True).start()
        else:
            threading.Thread(target=run_job,
                             args=(new, retry_render_job, job['project'], job['scene'],
                                   snap, new['take_id'], args),
                             daemon=True).start()
        return {'job': public(new), 'retry_of': job['id'], 'warning': retry_warning(job, verdict)}


def _retryable_row(job_id):
    """The retry route's fallback when loadable_job cannot help (ComfyUI unreachable, so its
    reconcile-first answers nothing): a persisted unresolved row -- 'unknown' or still in an
    open pre-terminal state a recovery pass could not settle -- rebuilt display-only with the
    late-record plumbing _settle would have set. retry_allowed follows the same evidence
    _lost_message uses (an open row with no prompt ids proves nothing ever landed), because
    retry_job() itself refuses the moment ComfyUI cannot be re-checked first."""
    try:
        row = jobs_db.get(job_id)
    except jobs_db.JobDbError:
        return None
    if not row or row['resolved']:
        return None
    if row['state'] != 'unknown' and row['state'] not in RECOVER_STATES:
        return None
    job = _rebuilt_job(row)
    job['retry_allowed'] = (bool(row['retry_allowed']) or not row['prompt_ids']
                            or row['state'] == jobs_db.SUBMITTING)
    job.update(state='unknown', done=True, ended=row['ended'] or time.time(),
               holds=[tuple(k) for k in (row['holds'] or [])])
    prep_late_record(job, recovery_snapshot(job))
    return job


def public(job):
    now = time.time()
    out = {k: job[k] for k in ('id', 'kind', 'error', 'done', 'state', 'take', 'final')}
    out['prompt_ids'] = list(job['prompt_ids'])
    out['resolved'] = job.get('resolved')
    out['retry_allowed'] = bool(job.get('retry_allowed'))
    out['retry_of'] = job.get('retry_of')
    out['retried_as'] = job.get('retried_as')
    out['notice'] = job.get('notice')
    out['stages'] = [{**s, 'seconds': round((s['ended'] or now) - s['started']) if s['started'] else None}
                     for s in job['stages']]
    out['elapsed'] = round(now - job['created'])
    return out


def decode(image):
    """{'name':..., 'data': 'data:...;base64,...'} -> (name, bytes)."""
    return image['name'], base64.b64decode(image['data'].split(',', 1)[-1])


# ---------------------------------------------------------------- film projects

BATCH = {}        # slug -> {'label', 'step', 'of', 'scene', 'job', 'error', 'done', 'started'}


def _files(paths):
    return [(Path(f).name, Path(f).read_bytes()) for f in paths]


def project_busy(slug):
    """True while a batch or any live job of this film is still running."""
    b = BATCH.get(slug)
    if b and not b['done']:
        return True
    return bool(live_jobs(slug))


def check_no_live_jobs(slug, what):
    if live_jobs(slug):
        raise Busy(f'busy: a job is running for this film; cannot {what}')


def check_keeps_reserved(slug, data):
    """A save that would drop scenes holding live jobs is refused outright: server-side,
    so it cannot be bypassed. A removal slipping in after this check only means the job's
    own scene_index fails it cleanly when it next touches the project."""
    if data.get('scenes_delta'):
        return                                       # a delta patch cannot remove scenes at all
    if isinstance(data.get('scenes'), list):
        keep = {str(s.get('id')) for s in data['scenes'] if isinstance(s, dict)}
        gone = sorted(reserved_scenes(slug) - keep)
        if gone:
            raise Busy('busy: ' + ', '.join(f'scene {sid} has a running job' for sid in gone))


def check_of(report):
    return {'retried': report['retried'], 'first_failures': report['first_failures'], 'final_failures': report['final_failures']}


def write_scene_prompt(slug, scene_id, think=True):
    """Director writes scene's H3 prompt from the bible + action (+ refs or the previous last frame);
    returns (mode, model) so a render job can name the director behind its snapshot."""
    p = projects.load(slug)
    i = projects.scene_index(p, scene_id)
    scene = p['scenes'][i]
    refs, first = projects.scene_media(p, i)
    if scene['connect'] == 'continue' and i and first is None:
        refs = []          # previous take not rendered yet: write I2VA-ready text, frame is attached at render
    report = director.direct_report(director.scene_idea(p, i), scene['seconds'], refs, first, None, think)
    mode, text = report['mode'], report['text']
    def apply(proj):
        s = proj['scenes'][projects.scene_index(proj, scene_id)]
        if s.get('prompt', '').strip() and not s.get('history'):
            projects.push_history(s, s['prompt'], s.get('prompt_mode'), 'edit')
        s['prompt'], s['prompt_mode'] = text, mode
        s['prompt_check'] = check_of(report)
        projects.push_history(s, text, mode, 'director')
    projects.mutate(slug, apply)
    return mode, report['model']


def scene_stage_keys(kind, settings=None):
    """Which web stages a job of `kind` runs: the finishing stages only for the switches the
    project's settings have on (encode is always on -- every finishing chain ends by encoding)."""
    fin = finishing.enabled_stages(settings or finishing.defaults())
    return {'render-draft': ['render'], 'render-final': ['render', *fin], 'enhance': [*fin]}[kind]


def validate_scene_media(p, i):
    """Reject a render whose prerequisites are missing before accepting the job, so a doomed
    render fails with a clear 4xx instead of dying mid-job."""
    for f in projects.ref_files(p):
        if not f.is_file():
            raise projects.ProjectError(f'reference picture missing: {f.name}')
    if p['scenes'][i]['connect'] == 'continue' and i:
        prev = projects.chosen(p['scenes'][i - 1])
        if prev:
            enhance.run_dir_of(prev['run'])   # raises FileNotFoundError; the handler answers 4xx


def continuation(p, i):
    """The take a continue-scene starts from, as chosen when its media was resolved: addressed by
    application id, or by run directory when the source take is legacy; none is invented."""
    if p['scenes'][i].get('connect') != 'continue' or not i:
        return None
    prev = projects.chosen(p['scenes'][i - 1])
    return prev and {'scene': p['scenes'][i - 1]['id'], 'take': projects.take_ref(prev), 'run': prev['run']}


def render_snapshot(job, slug, scene_id, take_id, preset_name, variant, args, graph, model, source):
    """The immutable record of what is about to be submitted: exact prompt, mode, settings and graph,
    plus a content-addressed copy of every input picture, so a take can be reconstructed field by
    field even after ComfyUI's output folder has been pruned. seed 0 is recorded as 0."""
    refs = [dict(projects.store_ref(slug, name, raw), index=n) for n, (name, raw) in enumerate(args['refs'])]
    first = None
    if args['first']:
        name, raw = args['first']
        first = dict(projects.store_ref(slug, name, raw), source=source)
    return {
        'id': job['id'], 'kind': job['kind'], 'created': job['created'], 'project': slug,
        'scene': str(scene_id), 'take_id': take_id, 'preset': preset_name,
        'variant': list(variant) if variant else None,
        'mode': director.select_mode(refs=args['refs'], first=args['first'], last=args['last']),
        'prompt': args['prompt'],
        'seed': args['seed'], 'width': args['width'], 'height': args['height'],
        'seconds': args['seconds'], 'two_pass': args['two_pass'],
        'refs': refs, 'first': first, 'loras': args['loras'], 'graph': graph,
        'director': {'model': model, 'skill_revision': director.skill_revision()},
        'instructions': projects.instructions_of(projects.load(slug)),
        'finishing': job_finishing(job.get('finishing')) if PRESETS[preset_name]['finish'] else None}


def source_take_of(snapshot):
    """For a continuation render, the take its first frame came from: the scene, that take's app id
    (its run directory when legacy), its run, and the hash of the frame this snapshot content-
    addressed. Read back from the snapshot this same job wrote, so the record on the take can never
    drift from what was actually used."""
    first = snapshot.get('first') or {}
    src = first.get('source')
    if not src:
        return None
    return {'scene': src['scene'], 'take': src['take'], 'run': src['run'],
            'frame_sha256': first.get('sha256')}


def render_job(job, slug, scene_id, preset_name, variant=None):
    """Thread body: render one scene (Draft or Final) and record the take (and final) in the project.

    The request snapshot (jobs/<job id>.json) is written once the graph is built and BEFORE it is
    submitted; the sibling result file lands when the job ends. See project.py, generation records."""
    reserve(job, [scene_key(slug, scene_id)])
    preset = PRESETS[preset_name]
    p = projects.load(slug)
    i = projects.scene_index(p, scene_id)
    scene = p['scenes'][i]
    model = None
    if not scene['prompt'].strip():
        _, model = write_scene_prompt(slug, scene_id)
        check_cancel(job)                      # never submit work the user already cancelled
        p = projects.load(slug)
        scene = p['scenes'][i]
    refs, first = projects.scene_media(p, i)
    prompt = projects.ensure_i2va(scene['prompt']) if first else scene['prompt']
    w, h = p['size']
    check_cancel(job)                          # ...and not after reading media either
    args = dict(prompt=prompt, seconds=scene['seconds'], width=w, height=h, seed=scene['seed'],
                refs=_files(refs), first=_files([first])[0] if first else None, last=None,
                two_pass=preset['two_pass'], loras=p.get('loras'))
    graph = build_graph(**args)                # uploads the pictures; submits only once recorded
    take_id = projects.new_take_id()
    job['take_id'] = take_id             # so the durable row can name the take it will create
    snapshot = render_snapshot(job, slug, scene_id, take_id, preset_name, variant,
                               args, graph, model, continuation(p, i))
    projects.write_job_snapshot(slug, snapshot)
    source_take = source_take_of(snapshot)     # a continuation names the take it started from
    on_take = take_recorder(slug, scene_id, job['id'], take_id, preset_name,
                            scene['seed'], variant, source_take)
    on_final = final_recorder(slug, scene_id, lambda: job['take']['run'], job['id'])
    job['late_record'] = render_late_record(job, on_take, on_final, take_id, preset['finish'])
    do_render(job, graph, preset['finish'], on_take, on_final)
    write_job_result(job, take_id)


def final_recorder(slug, scene_id, run_of, job_id=None):
    def on_final(final):
        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            for t in s['takes']:
                if t['run'] == run_of():
                    t['final'], t['final_url'] = final['path'], final['url']
                    if job_id:
                        # which job's finishing settings produced THIS final -- the compare
                        # in scene_status reads its snapshot; no id keeps legacy semantics
                        t['final_job'] = job_id
        projects.mutate(slug, apply)
    return on_final


def take_recorder(slug, scene_id, job_id, take_id, preset_name, seed, variant, source_take):
    """Append this job's take to its scene -- idempotent under the project lock: a take for
    this id, this job, or this exact run directory already recorded means nothing is appended
    twice, whether the proof comes live, late after reconcile, or from a recovery pass."""
    def on_take(take):
        duplicate = []

        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            if any(t.get('id') == take_id or t.get('job') == job_id for t in s['takes']):
                return                                   # already recorded (a resolved unknown job)
            same = next((t for t in s['takes'] if t.get('run') and t.get('run') == take['run']), None)
            if same is not None:
                # ComfyUI answered an identical request (same seed and inputs) from its cache: the output is the
                # video an earlier take already points at, so recording it again would show one file as two takes
                duplicate.append(same.get('id') or same.get('run'))
                return
            s['takes'].append({'id': take_id, 'job': job_id, 'provenance': 'recorded',
                               'run': take['run'], 'path': take['path'], 'url': take['url'], 'preset': preset_name,
                               'seed': seed, 'created': time.time(), 'final': None, 'final_url': None,
                               'variant': list(variant) if variant else None,
                               'source_take': source_take})
        projects.mutate(slug, apply)
        if duplicate:
            job = JOBS.get(job_id)
            if job is not None:
                job['notice'] = ('ComfyUI returned its cached result for an identical request (same seed and inputs), '
                                 f'so no new take was recorded: the video is take {duplicate[0]}. '
                                 'Use a new seed for another take.')
    return on_take


def render_late_record(job, on_take, on_final, take_id, finish_after):
    """What a finished render must record whenever the proof arrives -- live, or late after
    the job went unknown: the take, the final if the chain ran, the result file. Every write
    is replay-safe."""
    def late(verdict):                                   # an unknown job proved to have finished:
        if verdict != 'finished' or not job['prompt_ids']:
            return
        take = render_result(job['prompt_ids'][0])
        job['take'] = take
        on_take(take)
        if finish_after and len(job['prompt_ids']) > 1:
            final = enhance.result(job['prompt_ids'][-1])
            job['final'] = final
            on_final(final)
        try:
            write_job_result(job, take_id)               # written once; a clash means it already was
        except projects.ProjectError:
            pass
    return late


def start_batch(slug, label, steps, worker):
    """Run worker(step_callback) in a thread; one batch per project at a time."""
    current = BATCH.get(slug)
    if current and not current['done']:
        raise Busy(f'busy: {current["label"]} is still running')
    if live_jobs(slug):
        raise Busy('busy: a job is running for this film')
    state = {'label': label, 'step': 0, 'of': steps, 'scene': None, 'job': None, 'error': None, 'done': False,
             'started': time.time(), 'result': None}
    BATCH[slug] = state
    persist_batch(slug, state)
    def run():
        try:
            state['result'] = worker(state)
        except Exception as e:
            state['error'] = str(e)
        state['finished'] = time.time()
        persist_batch(slug, state, done=True)     # on disk first, so readers of done never see a stale file
        state['done'] = True
        notify_batch(slug, state)  # after done: a slow webhook must not hold the UI or the next batch
    threading.Thread(target=run, daemon=True).start()
    return state


def notify_batch(slug, state):
    """One notification when a batch ends: finished, failed or cancelled. Never raises."""
    try:
        err = state.get('error')
        outcome = 'cancelled' if err and str(err).endswith('cancelled') else 'failed' if err else 'finished'
        try:
            name = projects.load(slug).get('name') or slug
        except Exception:
            name = slug
        seconds = round((state.get('finished') or time.time()) - state['started'])
        body = f'{name} · step {state["step"]}/{state["of"]} · {seconds}s' + (f' · {err}' if outcome == 'failed' else '')
        notify.send(f'{state["label"]} {outcome}', body)
    except Exception:
        pass


def persist_batch(slug, state, done=None):
    """Record the batch in project.json so a restart can tell it died (projects.recover_interrupted)."""
    keep = {k: state.get(k) for k in ('label', 'step', 'of', 'done', 'error', 'started', 'finished')}
    if done is not None:
        keep['done'] = done
    try:
        projects.mutate(slug, lambda p: p.__setitem__('last_batch', keep))
    except projects.ProjectError:
        pass


def cancel_project(slug):
    """Stop this project's running batch and jobs: flag them, then tell ComfyUI to drop only our prompts."""
    state = BATCH.get(slug)
    if state and not state['done']:
        state['cancel'] = True
    ours = set()
    for job in live_jobs(slug):                 # every live job of this film, however it started
        job['cancel'] = True
        ours.update(job['prompt_ids'])
    if not ours:
        return
    target_cancel(ours)                                # our prompts only; unconfirmed ones surface as unknown in the job threads


def batch_prompts(slug, only_missing):
    def worker(state):
        p = projects.load(slug)
        todo = [s['id'] for s in p['scenes'] if not (only_missing and s['prompt'].strip())]
        state['of'] = len(todo)
        for n, sid in enumerate(todo, 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            state.update(step=n, scene=sid)
            write_scene_prompt(slug, sid)
    return start_batch(slug, 'Writing prompts', 0, worker)


def batch_render(slug, preset_name, only_missing):
    def worker(state):
        p = projects.load(slug)
        todo = [s['id'] for s in p['scenes'] if not (only_missing and s['takes'])]
        st = finishing.for_project(p)
        state['of'] = len(todo)
        for n, sid in enumerate(todo, 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            kind = 'render-' + preset_name
            job = new_job(kind, scene_stage_keys(kind, st), project=slug, scene=sid, settings=st)
            state.update(step=n, scene=sid, job=job['id'])
            run_job(job, render_job, slug, sid, preset_name)
            if job['error']:
                raise RuntimeError(f'scene {n}: {job["error"]}')
    return start_batch(slug, 'Drafting scenes' if preset_name == 'draft' else 'Rendering finals', 0, worker)


def batch_variants(slug, scene_id, n):
    def worker(state):
        state['of'] = n
        for k in range(1, n + 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            p = projects.load(slug)
            scene = p['scenes'][projects.scene_index(p, scene_id)]
            if k > 1 or scene['takes']:
                projects.mutate(slug, lambda pr: projects.reroll_seed(pr, scene_id))
            job = new_job('render-draft', scene_stage_keys('render-draft'), project=slug, scene=scene_id)
            state.update(step=k, scene=scene_id, job=job['id'])
            run_job(job, render_job, slug, scene_id, 'draft', variant=(k, n))
            if job['error']:
                raise RuntimeError(f'variant {k}: {job["error"]}')
    return start_batch(slug, 'Drafting variants', n, worker)


def batch_enhance(slug):
    def worker(state):
        p = projects.load(slug)
        todo = [(s['id'], projects.chosen(s)) for s in p['scenes']]
        todo = [(sid, t) for sid, t in todo if t and not t.get('final')]
        state['of'] = len(todo)
        st = finishing.for_project(p)
        for n, (sid, take) in enumerate(todo, 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            job = new_job('enhance', scene_stage_keys('enhance', st), project=slug, scene=sid,
                          settings=st)
            state.update(step=n, scene=sid, job=job['id'])
            run_job(job, enhance_job, take['run'],
                    final_recorder(slug, sid, lambda run=take['run']: run, job['id']))
            if job['error']:
                raise RuntimeError(f'scene {n}: {job["error"]}')
    return start_batch(slug, 'Enhancing ★ takes', 0, worker)


def batch_assemble(slug, kind):
    def worker(state):
        state['of'] = 1
        state['step'] = 1
        out = projects.assemble(projects.load(slug), kind, cancel=lambda: bool(state.get('cancel')))
        projects.mutate(slug, lambda proj: proj['outputs'].__setitem__(kind, out))
        return out
    return start_batch(slug, 'Assembling the film' if kind == 'film' else 'Assembling the animatic', 1, worker)


def project_view(slug):
    p = projects.load(slug)
    for s in p['scenes']:
        jid = SCENE_JOB.get((slug, s['id']))
        s['job'] = public(JOBS[jid]) if jid in JOBS else None
        c = projects.chosen(s)
        s['chosen'] = c['run'] if c else None
        ref, mode = projects.selection_of(s)   # derived: like provenance, a GET stores nothing
        s['selected_take'] = ref
        s['selection'] = mode
        s['status'] = projects.scene_status(p, s)   # derived per request; never stored (project.py)
        for t in s.get('takes') or []:          # derived: legacy takes never grow stored keys
            t['provenance'] = projects.take_provenance(t)
            t['selected'] = t is c
    for kind, out in (p.get('outputs') or {}).items():
        out.update(projects.output_status(p, kind, out))   # derived per request; never stored (project.py)
    p['finishing'] = finishing.for_project(p)   # derived per request; never stored: an absent
    p['source_fps'] = enhance.FPS               # finishing key stays absent until an editor save
    b = BATCH.get(slug)
    p['batch'] = dict(b, seconds=round(time.time() - b['started'])) if b else None
    p['batch_job'] = public(JOBS[b['job']]) if b and b.get('job') in JOBS else None
    return p


def take_view(slug, take_ref):
    """GET /api/p/<slug>/take?id=...: for a recorded take its immutable snapshot plus result; for a
    pre-records take just the take and 'legacy'. Legacy takes have no application id, so they are
    addressed by their run directory."""
    p = projects.load(slug)
    for s in p['scenes']:
        for t in s.get('takes') or []:
            if take_ref not in (t.get('id'), t.get('run')):
                continue
            if projects.take_provenance(t) == 'legacy':
                return {'provenance': 'legacy', 'scene': s['id'], 'take': t}
            snapshot, result = projects.read_job(slug, t['job']) if t.get('job') else (None, None)
            return {'provenance': 'recorded', 'scene': s['id'], 'take': t,
                    'snapshot': snapshot, 'result': result}
    raise projects.ProjectError('no such take')

def poster_view(slug, take_ref):
    """GET /api/p/<slug>/poster?id=...: a poster frame (JPG) for a take, made on demand with ffmpeg
    and cached under the project's frames/ (gitignored, like the continuity frames). Resolves the
    take exactly like take_view (id or run dir), prefers the project's own media copy so a poster
    works after the ComfyUI output folder has been pruned. Raises ProjectError when the take or a
    source video is unknown, RuntimeError when ffmpeg cannot produce a frame."""
    p = projects.load(slug)
    take = None
    for s in p['scenes']:
        for t in s.get('takes') or []:
            if take_ref in (t.get('id'), t.get('run')):
                take = t
                break
        if take:
            break
    if not take:
        raise projects.ProjectError('no such take')
    candidates = []
    for key in ('final_url', 'url'):                    # enhanced take first: its own frame
        url = take.get(key)
        if isinstance(url, str) and url.startswith('/files/'):
            parts = urllib.parse.unquote(url[len('/files/'):]).split('/')
            if len(parts) >= 2 and all(x and x not in ('.', '..') for x in parts):
                candidates.append(projects.path(*parts))
    for key in ('final', 'path'):                       # then the run-dir files themselves
        if take.get(key):
            candidates.append(Path(take[key]))
    video = next((f for f in candidates if f.is_file()), None)
    if video is None:
        raise projects.ProjectError('take video unavailable')
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', take.get('id') or Path(str(take['run']).rstrip('/')).name)
    out = projects.path(slug, 'frames', f'{safe}-poster.jpg')
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([FFMPEG, '-v', 'error', '-y', '-ss', '1', '-i', str(video),
                            '-frames:v', '1', '-vf', 'scale=320:-1', '-q:v', '6', str(out)])
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError(f'ffmpeg poster frame failed for {video.name}')
    return out

def library_view(c):
    """Library reply row: the stored fields plus the URL for the character's picture."""
    return dict(c, image_url=f'/library-files/{c["slug"]}/{c["image"]}' if c.get('image') else None)


RECENT_DONE_SECONDS = 30 * 60   # finished jobs stay on the queue board this long


def queue_view():
    """The queue board: live jobs (with learned estimates), recently finished jobs, and batches.

    Live jobs are memory-only; finished verdicts additionally come back from the durable
    store (jobs_db) after a restart, flagged `persisted`, for exactly as long as they would
    have stayed on the board anyway. Work that was still unfinished when the director died is
    re-checked against ComfyUI at startup and periodically (recover_jobs): reattached, settled
    from history, or left 'unknown' -- never resubmitted. Each job row
    carries the raw epochs (created/started/ended) plus two SEPARATE durations: queued_for
    (accepted -> ComfyUI first verifiably had the work) and running_for (that moment -> now/end).
    A job that never reached ComfyUI has running_for null and is still marked cancelled/failed."""
    now = time.time()
    def film(slug, scene_id=None):
        try:
            p = projects.load(slug)
        except Exception:
            return None, None
        scene = next((s for s in p['scenes'] if str(s['id']) == scene_id), None)
        return p.get('name') or slug, scene['title'] if scene else None
    live, done = [], []
    for jid, job in list(JOBS.items()):
        unresolved = job['state'] == 'unknown' and bool(unresolved_owner(job['id']))
        recent = job['done'] and (job.get('ended') or 0) > now - RECENT_DONE_SECONDS
        if not (not job['done'] or unresolved or recent):
            continue                                     # old verdicts leave the board
        slug, scene_id = job.get('project'), job.get('scene')
        name, title = film(slug, scene_id) if slug else (None, None)
        end = job['ended'] if job['done'] else now
        estimate = stats.estimate(job['kind'])
        estimate = round(estimate) if estimate is not None else None
        started = job.get('started')
        row = {'id': jid, 'kind': job['kind'], 'state': job['state'], 'project': slug,
               'project_name': name, 'scene': scene_id, 'scene_title': title,
               'elapsed': round(end - job['created']),
               'estimate': estimate,
               'eta': max(0, estimate - round(end - job['created'])) if estimate is not None and not job['done'] else None,
               'stage': next((s['label'] for s in job['stages'] if s['state'] == 'active'), None),
               'error': job['error'], 'prompt_ids': list(job['prompt_ids']),
               'resolved': job.get('resolved'), 'live': not job['done'], 'correlation': job.get('correlation'),
               'take': (job.get('take') or {}).get('run'),
               'created': job.get('created'), 'started': started, 'ended': job.get('ended'),
               'queued_for': round((started or end) - job['created']),
               'running_for': round(end - started) if started else None,
               'retry_allowed': bool(job.get('retry_allowed')), 'recovering': bool(job.get('recovering_at')),
               'retry_of': job.get('retry_of'), 'retried_as': job.get('retried_as')}
        (live if row['live'] else done).append(row)
    live.sort(key=lambda r: r['created'] or 0)             # oldest live job first
    done.sort(key=lambda r: -(r['ended'] or 0))           # newest verdict first
    # finished verdicts outlive a restart through the durable store; live jobs do not.
    # Additive row fields only: persisted, correlation.
    seen = {r['id'] for r in live} | {r['id'] for r in done}
    try:
        for row in jobs_db.list_jobs(states=TERMINAL, since=now - RECENT_DONE_SECONDS, limit=200):
            if row['id'] in seen:
                continue                                     # the memory view already shows it
            slug, scene_id = row['project'], row['scene']
            name, title = film(slug, scene_id) if slug else (None, None)
            end = row['ended'] or now
            started = row['started']
            estimate = stats.estimate(row['kind'])
            estimate = round(estimate) if estimate is not None else None
            done.append({'id': row['id'], 'kind': row['kind'], 'state': row['state'], 'project': slug,
                         'project_name': name, 'scene': scene_id, 'scene_title': title,
                         'elapsed': round(end - (row['created'] or end)),
                         'estimate': estimate, 'eta': None, 'stage': None, 'error': row['error'],
                         'prompt_ids': row['prompt_ids'], 'resolved': row['resolved'], 'live': False,
                         'take': row['run'], 'created': row['created'], 'started': started, 'ended': row['ended'],
                         'queued_for': round((started or end) - (row['created'] or end)),
                         'running_for': round(end - started) if started else None,
                         'correlation': row['correlation'], 'persisted': True,
                         'retry_allowed': bool(row['retry_allowed']), 'recovering': False,
                         'retry_of': row['retry_of'], 'retried_as': row['retried_as']})
    except Exception as e:
        print(f'jobs: durable queue history unavailable: {e}')  # a broken store never breaks the board
    # a recovery pass that could not reach ComfyUI leaves its rows in the store marked
    # recovering: shown as unknown "not reachable yet", state and holds untouched.
    try:
        for row in jobs_db.list_jobs(states=RECOVER_STATES, limit=200):
            if row['id'] in seen:
                continue                                     # the memory view already shows it
            slug, scene_id = row['project'], row['scene']
            name, title = film(slug, scene_id) if slug else (None, None)
            done.append({'id': row['id'], 'kind': row['kind'], 'state': 'unknown', 'project': slug,
                         'project_name': name, 'scene': scene_id, 'scene_title': title,
                         'elapsed': round(now - (row['created'] or now)),
                         'estimate': None, 'eta': None, 'stage': None,
                         'error': f'{NOT_REACHABLE}: the director re-checks ComfyUI every '
                                  f'{RECONCILE_SECONDS}s; nothing was resubmitted.',
                         'prompt_ids': row['prompt_ids'], 'resolved': None, 'live': False,
                         'take': row['run'], 'created': row['created'], 'started': row['started'],
                         'ended': None, 'queued_for': None, 'running_for': None,
                         'correlation': row['correlation'], 'persisted': True,
                         'retry_allowed': bool(row['retry_allowed']), 'recovering': True,
                         'retry_of': row['retry_of'], 'retried_as': row['retried_as']})
    except Exception as e:
        print(f'jobs: durable recovery rows unavailable: {e}')  # a broken store never breaks the board
    batches = []
    for slug, b in list(BATCH.items()):
        if not b['done']:
            batches.append({'project': slug, 'project_name': film(slug)[0], 'label': b['label'], 'step': b['step'],
                            'of': b['of'], 'scene': b['scene'], 'seconds': round(now - b['started'])})
    return {'jobs': live + done, 'batches': batches}


def serve_file(handler, file):
    """Static file with Range support (browser seeking in mp4)."""
    size = file.stat().st_size
    kind = {'.mp4': 'video/mp4', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg',
            '.flac': 'audio/flac'}.get(file.suffix.lower(), 'application/octet-stream')
    start, end = 0, size - 1
    rng = handler.headers.get('Range', '')
    m = re.match(r'bytes=(\d*)-(\d*)', rng)
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start = size - int(m.group(2))
        end = min(end, size - 1)
        handler.send_response(206)
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    else:
        handler.send_response(200)
    handler.send_header('Content-Type', kind)
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Content-Length', str(end - start + 1))
    handler.end_headers()
    try:
        with open(file, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
    except (ConnectionResetError, BrokenPipeError):       # the browser closed the connection (seeking in a video)
        return


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    extra_hosts = set()

    def check_access(self):
        """False after 403 when Host is missing/not allowed, or Origin is present and not same-origin."""
        port = self.server.server_port
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}'} | {h.lower() for h in self.extra_hosts}
        host = (self.headers.get('Host') or '').strip().lower()
        origin = self.headers.get('Origin')
        if host not in allowed or (origin and origin.strip().lower() not in {f'http://{a}' for a in allowed}):
            return self.reply(403, {'error': 'forbidden host/origin'}) or False
        return True

    def reply(self, code, payload, kind='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.check_access():
            return
        if self.path in ('/', '/index.html'):
            return self.reply(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path in ('/project', '/project.html') or self.path.startswith('/project?'):
            return self.reply(200, (HERE / 'project.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/project/classic' or self.path.startswith('/project/classic?'):
            return self.reply(200, (HERE / 'project-classic.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/api/queue':
            return self.reply(200, queue_view())
        if self.path == '/api/config':
            return self.reply(200, config.api_view())
        if self.path == '/api/readiness' or self.path.startswith('/api/readiness?'):
            # ?project=<slug> scores the finishing check with THAT film's settings: a stage the
            # project switched off cannot make its chip red. Without it every stage counts.
            q = urllib.parse.parse_qs(self.path.partition('?')[2])
            slug = (q.get('project') or [''])[0]
            try:
                st = finishing.for_project(projects.load(slug)) if slug else None
            except projects.ProjectError:
                return self.reply(404, {'error': 'no such film'})
            return self.reply(200, readiness.report(settings=st))
        route = self.path.partition('?')[0]
        if route.startswith('/api/readiness/'):
            name = route[len('/api/readiness/'):]
            if name in readiness.CHECKS:
                q = urllib.parse.parse_qs(self.path.partition('?')[2])
                refresh = (q.get('refresh') or [''])[0] == '1'
                slug = (q.get('project') or [''])[0]
                try:
                    st = finishing.for_project(projects.load(slug)) if slug else None
                except projects.ProjectError:
                    return self.reply(404, {'error': 'no such film'})
                return self.reply(200, readiness.check(name, refresh=refresh, settings=st))
        if self.path == '/queue':
            return self.reply(200, (HERE / 'queue.html').read_bytes(), 'text/html; charset=utf-8')
        if self.path == '/api/loras':
            try:
                spec = comfy('/object_info/FreeVideoLoRA')['FreeVideoLoRA']['input']['required']['lora']
                names = spec[1]['options'] if spec[0] == 'COMBO' else spec[0]      # newer ComfyUI: ['COMBO', {'options': [...]}]
            except (OSError, KeyError, IndexError, ValueError) as e:
                return self.reply(502, {'error': f'ComfyUI LoRA list unavailable at {COMFY}: {e}'})
            return self.reply(200, {'loras': [n for n in names if n != 'None']})
        if self.path == '/api/library':
            return self.reply(200, [library_view(c) for c in library.list_all()])
        if self.path.startswith('/library-files/'):
            parts = urllib.parse.unquote(self.path[len('/library-files/'):]).split('/')
            if len(parts) != 2 or parts[1] not in library.IMAGE_FILES:
                return self.reply(404, {'error': 'not found'})
            try:
                image = library.image_path(parts[0])
            except library.LibraryError:
                return self.reply(404, {'error': 'not found'})
            if image is None or image.name != parts[1] or not image.is_file():
                return self.reply(404, {'error': 'not found'})
            return serve_file(self, image)
        try:
            if self.path == '/api/projects':
                return self.reply(200, projects.listing())
            if self.path.startswith('/api/p/'):
                route, _, query = self.path.partition('?')
                parts = route.split('/')
                if len(parts) == 4:
                    return self.reply(200, project_view(parts[3]))
                if len(parts) == 5 and parts[4] == 'take':
                    tid = (urllib.parse.parse_qs(query).get('id') or [''])[0]
                    return self.reply(200, take_view(parts[3], tid))
                if len(parts) == 5 and parts[4] == 'poster':
                    pid = (urllib.parse.parse_qs(query).get('id') or [''])[0]
                    try:
                        return serve_file(self, poster_view(parts[3], pid))
                    except RuntimeError as e:                # ffmpeg could not make a frame
                        return self.reply(502, {'error': str(e)})
                return self.reply(404, {'error': 'not found'})
            if self.path.startswith('/files/'):
                parts = urllib.parse.unquote(self.path[len('/files/'):]).split('/')
                if len(parts) < 2 or any(not p or p in ('.', '..') for p in parts):
                    return self.reply(404, {'error': 'not found'})
                root = projects.path(parts[0]).resolve()
                file = projects.path(*parts).resolve()
                if not file.is_relative_to(root) or not file.is_file():
                    return self.reply(404, {'error': 'not found'})
                return serve_file(self, file)
        except projects.ProjectError as e:
            return self.reply(404, {'error': str(e)})
        if self.path.startswith('/api/job/'):
            job = JOBS.get(self.path.rsplit('/', 1)[-1])
            return self.reply(200, public(job)) if job else self.reply(404, {'error': 'unknown job'})
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.check_access():
            return
        if (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
            return self.reply(415, {'error': 'use application/json'})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            refs = [decode(x) for x in data.get('refs', [])]
            first = decode(data['first']) if data.get('first') else None
            last = decode(data['last']) if data.get('last') else None
            if self.path == '/api/direct':
                readiness.require('prompt')
                start = time.time()
                report = director.direct_report(
                    data.get('idea', ''), float(data.get('seconds', 10)), [r for _, r in refs],
                    first and first[1], last and last[1], bool(data.get('think', True)))
                mode, text, tokens, model = report['mode'], report['text'], report['tokens'], report['model']
                saved = director.save(text)
                return self.reply(200, {'mode': mode, 'model': model, 'prompt': text, 'tokens': tokens,
                                        'saved': str(saved), 'elapsed': round(time.time() - start),
                                        'check': check_of(report)})
            if self.path == '/api/render':
                preset = PRESETS.get(data.get('preset', 'draft'))
                if not preset:
                    return self.reply(400, {'error': 'unknown preset'})
                if not str(data.get('prompt', '')).strip():
                    return self.reply(400, {'error': 'write or paste a prompt first'})
                readiness.require('video')
                if preset['finish']:
                    readiness.require('finishing')
                loras = clean_loras(data.get('loras'))
                job = new_job(data.get('preset', 'draft'),
                              scene_stage_keys('render-final') if preset['finish'] else ['render'])
                args = dict(prompt=data['prompt'], seconds=data.get('seconds', 10),
                            width=int(data.get('width', 1344)), height=int(data.get('height', 768)),
                            seed=int(data.get('seed') or time.time_ns() % 2**31), refs=refs, first=first,
                            last=last, two_pass=preset['two_pass'], loras=loras)
                threading.Thread(target=run_job, args=(job, do_render_adhoc, args, preset['finish']), daemon=True).start()
                return self.reply(200, public(job))
            if self.path == '/api/library':
                img = data.get('image')
                raw = None
                if img is not None:
                    if not isinstance(img, dict):
                        raise library.LibraryError('image must be an object')
                    raw = decode(img)[1]
                return self.reply(200, library_view(library.save(data.get('name'), data.get('description'), raw)))
            if self.path.startswith('/api/library/') and self.path.endswith('/delete'):
                slug = urllib.parse.unquote(self.path.split('/')[3])
                try:
                    library.get(slug)
                except library.LibraryError:
                    return self.reply(404, {'error': 'no such character'})
                return self.reply(200, library.delete(slug))
            if self.path == '/api/projects':
                readiness.require('prompt')            # creating a film plans its storyboard
                created = projects.create(data.get('name', ''), data.get('logline', ''), data.get('seconds', 30),
                                          data.get('aspect', '16:9'), refs, data.get('characters', ()))
                plan, model = director.storyboard(created['logline'], created['seconds'],
                                                  projects.ref_files(created), created['aspect'],
                                                  character_notes=created.get('character_notes', ''))
                proj = projects.mutate(created['slug'], lambda pr: projects.apply_storyboard(pr, plan))
                return self.reply(200, dict(proj, model=model))
            if self.path.startswith('/api/p/'):
                return self.project_action(self.path.split('/')[3], self.path.split('/')[4], data)
            if self.path == '/api/enhance':
                readiness.require('finishing')
                run = data.get('run')
                if not run:
                    return self.reply(400, {'error': 'which take? (run directory missing)'})
                enhance.run_dir_of(run)                       # fail fast if it is not a FreeVideo take
                job = new_job('enhance', scene_stage_keys('enhance'))
                holder = reserve(job, [take_key(run)], reuse=True)
                if holder:                             # duplicate click: hand back the live job
                    abandon(job)
                    return self.reply(200, public(holder))
                threading.Thread(target=run_job, args=(job, enhance_job, run), daemon=True).start()
                return self.reply(200, public(job))
            m = re.match(r'^/api/job/([^/]+)/resolve$', self.path)
            if m:
                job = loadable_job(m.group(1))
                if not job:
                    return self.reply(404, {'error': 'unknown job'})
                if not job['done']:
                    return self.reply(409, {'error': 'the job is still running; cancel it instead'})
                if job['state'] != 'unknown':
                    return self.reply(400, {'error': 'only a job whose ComfyUI state is unknown can be resolved'})
                proved = reconcile(job)
                if not job.get('resolved'):              # nothing provable: the user calls it anyway
                    release_resolved(job, 'dismissed', by='user')
                return self.reply(200, {
                    'job': public(job), 'proved': proved,
                    'warning': 'the ComfyUI prompt may still be running: starting new work for this '
                               'scene/take could generate a duplicate take'})
            m = re.match(r'^/api/job/([^/]+)/reconcile$', self.path)
            if m:
                job = loadable_job(m.group(1))
                if not job:
                    return self.reply(404, {'error': 'unknown job'})
                if not job['done']:
                    return self.reply(409, {'error': 'the job is still running; cancel it instead'})
                if job['state'] != 'unknown':
                    return self.reply(400, {'error': 'only a job whose ComfyUI state is unknown can be reconciled'})
                # ask ComfyUI and report; unlike /resolve this never dismisses on its own --
                # only a proven verdict releases the holds (reconcile(), web.py)
                return self.reply(200, {'job': public(job), 'proved': reconcile(job)})
            m = re.match(r'^/api/job/([^/]+)/retry$', self.path)
            if m:
                # the explicit door for an unknown job whose submit provably never landed
                # (retry_allowed): replay its RECORDED request, once. Never automatic -- this
                # route is retry_job()'s one and only caller.
                old = loadable_job(m.group(1)) or _retryable_row(m.group(1))
                if not old:
                    return self.reply(404, {'error': 'unknown job'})
                if not old['done']:
                    return self.reply(409, {'error': 'the job is still running; cancel it instead'})
                if old['state'] != 'unknown' or old.get('resolved'):
                    return self.reply(400, {'error': 'only a job whose ComfyUI state is unknown can be retried'})
                if not old.get('retry_allowed'):
                    return self.reply(409, {'error': "no retry: ComfyUI had acknowledged this job's submit, "
                                                     'so the work may have completed and been purged or be '
                                                     'still running; resubmitting on that guess could double '
                                                     'the GPU work. Reconcile or dismiss it instead'})
                return self.reply(200, retry_job(old))
            m = re.match(r'^/api/job/([^/]+)/cancel$', self.path)
            if m:
                job = loadable_job(m.group(1))
                if not job:
                    return self.reply(404, {'error': 'unknown job'})
                if job['state'] == 'unknown':
                    return self.reply(409, {'error': 'job state unknown: reconcile or dismiss it instead'})
                if job['done']:
                    return self.reply(409, {'error': f'the job already {job["state"]}'})
                with LOCK:
                    job['cancel'] = True
                if job['prompt_ids']:
                    # ComfyUI verifiably holds the work: ask it to drop/interrupt; the worker
                    # proves the stop and finalises the job (run_job's Cancelled path)
                    stopped = target_cancel(job['prompt_ids'])
                    return self.reply(200, {'job': public(job), 'proved': stopped,
                                            'warning': None if stopped else
                                            'ComfyUI did not confirm the stop: the job stays live '
                                            'until the stop is proven'})
                # nothing ever reached ComfyUI: the cancel is provable right here
                cancel_job(job)
                return self.reply(200, {'job': public(job), 'proved': True})
            self.reply(404, {'error': 'not found'})
        except (director.DirectorError, projects.ProjectError, library.LibraryError) as e:
            self.reply(400, {'error': str(e)})
        except Busy as e:
            self.reply(409, {'error': str(e)})                # reserved scene/take, or a refused prerequisite
        except readiness.NotReady as e:
            self.reply(503, {'error': str(e)})         # the check names the missing items
        except (KeyError, ValueError, TypeError) as e:
            self.reply(400, {'error': f'bad request: {e}'})
        except FileNotFoundError as e:
            self.reply(400, {'error': str(e)})
        except urllib.error.HTTPError as e:
            self.reply(502, {'error': f'ComfyUI rejected the job: {e.read().decode()[:800]}'})
        except OSError as e:
            self.reply(502, {'error': f'ComfyUI unreachable at {COMFY}: {e}'})

    def project_action(self, slug, action, data):
        if action == 'save':                         # base_rev: the rev the editor was loaded at (optional)
            check_keeps_reserved(slug, data)         # a save must not silently delete a scene with a live job
            project, fields = projects.save_editor(slug, data, data.get('base_rev'))
            if fields:                               # the film changed underneath this editor: keep its draft
                return self.reply(409, {'error': 'the film changed while you were editing', 'fields': fields,
                                        'project': project_view(slug)})
            return self.reply(200, project)
        if action == 'storyboard':                     # re-plan: replaces scenes (the page confirms first)
            readiness.require('prompt')
            check_no_live_jobs(slug, 're-plan over a running job')
            p = projects.load(slug)
            plan, _ = director.storyboard(data.get('logline') or p['logline'], p['seconds'],
                                          projects.ref_files(p), p['aspect'], character_notes=p.get('character_notes', ''))
            def apply(proj):
                proj['logline'] = data.get('logline') or proj['logline']
                projects.apply_storyboard(proj, plan)
            return self.reply(200, projects.mutate(slug, apply))
        if action == 'prompt':                         # one scene, synchronous (~15 s)
            if str(data.get('scene')) in reserved_scenes(slug):
                raise Busy(f"busy: scene {data.get('scene')} has a running job")
            readiness.require('prompt')                # availability only once the scene is free
            write_scene_prompt(slug, data['scene'])
            return self.reply(200, project_view(slug))
        if action == 'prompts':
            readiness.require('prompt')
            batch_prompts(slug, bool(data.get('only_missing', True)))
            return self.reply(200, project_view(slug))
        if action == 'render':                         # one scene
            preset = data.get('preset', 'draft')
            if preset not in PRESETS:
                raise projects.ProjectError('unknown preset')
            p = projects.load(slug)
            i = projects.scene_index(p, data['scene'])   # unknown scene -> 400 before anything is accepted
            validate_scene_media(p, i)
            readiness.require('video')
            if PRESETS[preset]['finish']:
                st = finishing.for_project(p)
                readiness.require('finishing', settings=st)
            kind = 'render-' + preset
            job = new_job(kind, scene_stage_keys(kind, st if PRESETS[preset]['finish'] else None),
                          project=slug, scene=data['scene'],
                          settings=st if PRESETS[preset]['finish'] else None)
            try:
                reserve(job, [scene_key(slug, data['scene'])])
            except Busy:
                abandon(job)                             # never leave a job that was refused at the door
                raise
            threading.Thread(target=run_job, args=(job, render_job, slug, data['scene'], preset), daemon=True).start()
            return self.reply(200, project_view(slug))
        if action == 'enhance':                        # one scene's take
            p = projects.load(slug)
            scene = p['scenes'][projects.scene_index(p, data['scene'])]
            st = finishing.for_project(p)
            readiness.require('finishing', settings=st)   # after the request checks, like render
            run = data.get('run') or (projects.chosen(scene) or {}).get('run')
            if not run:
                raise projects.ProjectError('this scene has no take yet')
            enhance.run_dir_of(run)                    # fail fast if it is not a FreeVideo take
            job = new_job('enhance', scene_stage_keys('enhance', st), project=slug, scene=data['scene'],
                          settings=st)
            holder = reserve(job, [take_key(run), scene_key(slug, data['scene'])], reuse=True)
            if holder:                                 # duplicate click: the live job already shows on the scene
                abandon(job)
                return self.reply(200, project_view(slug))
            threading.Thread(target=run_job,
                             args=(job, enhance_job, run,
                                   final_recorder(slug, data['scene'], lambda: run, job['id']),
                                   [scene_key(slug, data['scene'])]),
                             daemon=True).start()
            return self.reply(200, project_view(slug))
        if action == 'characters':                     # change the film's library characters
            slugs = data.get('characters')
            if any(j['kind'].startswith('render') for j in live_jobs(slug)):
                raise Busy('busy: a render is running; it reads the current character pictures')
            return self.reply(200, projects.mutate(slug, lambda p: projects.set_characters(p, slugs)))
        if action == 'reroll':                         # new seed for the next Draft / Final of this scene
            projects.mutate(slug, lambda p: projects.reroll_seed(p, data['scene']))
            return self.reply(200, project_view(slug))
        if action == 'cancel':
            cancel_project(slug)
            return self.reply(200, project_view(slug))
        if action == 'restore_prompt':                 # history entry -> this scene's prompt
            projects.mutate(slug, lambda p: projects.restore_prompt(p, data['scene'], data['index']))
            return self.reply(200, project_view(slug))
        if action == 'prune':                          # dry run unless apply is true; moves, never deletes
            apply = data.get('apply', False)
            if type(apply) is not bool:
                raise projects.ProjectError('apply must be true or false')
            if not apply:
                return self.reply(200, dict(project_view(slug), prune=dict(projects.prune_plan(projects.load(slug)), applied=False)))
            if project_busy(slug):
                raise projects.ProjectError('busy: a render or batch is running for this film')
            result = {}
            projects.mutate(slug, lambda p: result.update(projects.prune_apply(p)))
            return self.reply(200, dict(project_view(slug), prune=dict(result, applied=True)))
        if action == 'star':                    # choose a take explicitly, by app id (or run for a
            ref = data.get('take') or data.get('run')   # legacy take); {"auto": true} releases the
            if data.get('auto') or ref is None:         # pick and the scene follows its latest take
                projects.mutate(slug, lambda p: projects.set_auto(p, data['scene']))
            else:
                projects.mutate(slug, lambda p: projects.set_explicit(p, data['scene'], ref))
            return self.reply(200, project_view(slug))
        if action == 'variants':                      # one batch of n Draft renders of a single scene
            readiness.require('video')
            n = data.get('n')
            if type(n) is not int or not 2 <= n <= 6:
                raise projects.ProjectError('n must be an integer from 2 to 6')
            projects.scene_index(projects.load(slug), data['scene'])   # validate before anything starts
            batch_variants(slug, data['scene'], n)
            return self.reply(200, project_view(slug))
        if action == 'draft-all':
            readiness.require('video')
            batch_render(slug, 'draft', bool(data.get('only_missing', True)))
            return self.reply(200, project_view(slug))
        if action == 'enhance-starred':
            readiness.require('finishing', settings=finishing.for_project(projects.load(slug)))
            batch_enhance(slug)
            return self.reply(200, project_view(slug))
        if action == 'assemble':
            readiness.require('finishing', settings=finishing.for_project(projects.load(slug)))
            if data.get('kind') not in ('animatic', 'film'):
                raise projects.ProjectError('kind must be animatic or film')
            batch_assemble(slug, data['kind'])
            return self.reply(200, project_view(slug))
        if action == 'music':
            def apply(p):
                for old in projects.path(slug).glob('music.*'):
                    old.unlink()
                if data.get('remove'):
                    p['music_file'] = None
                    return
                name, raw = decode(data['file'])
                target = projects.path(slug, 'music' + (Path(name).suffix.lower() or '.mp3'))
                target.write_bytes(raw)
                p['music_file'] = target.name
            projects.mutate(slug, apply)
            return self.reply(200, project_view(slug))
        self.reply(404, {'error': 'unknown action'})

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default=config.value('ui_host'))
    p.add_argument('--port', type=int, default=config.value('ui_port'))
    p.add_argument('--allow-host', action='append', default=[], metavar='NAME',
                   help='extra host name to admit (repeatable)')
    config.add_service_args(p)
    a = p.parse_args()
    Handler.extra_hosts = set(a.allow_host) | set(config.value('ui_allow_hosts'))
    projects.recover_interrupted()
    jobs_db.init()                       # the durable job bookkeeping, pruned to its retention
    jobs_db.prune()
    sweep_enhance_tmp()
    recover_jobs()      # re-check ComfyUI for every unfinished job before new work can start
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=ws_listen, daemon=True).start()
    print(f'FreeVideo director: {config.summary()}')
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
