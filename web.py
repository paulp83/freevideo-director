#!/usr/bin/env python3
"""Browser UI for the FreeVideo director.  http://127.0.0.1:8192

Idea + images -> H3 prompt (director.direct) -> render on the FreeVideo ComfyUI (:8190) ->
optional Final finishing (enhance.py: SeedVR2 upscale, RIFE 48 fps, CRF 16). Every render is a
job with visible stages and live progress (ComfyUI websocket). Stdlib only.
"""
import argparse, base64, json, os, re, socket, struct, threading, time, urllib.error, urllib.parse, \
    urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import director
import library
import notify
import stats
import enhance
import project as projects

HERE = Path(__file__).resolve().parent
COMFY = enhance.COMFY                          # COMFY_URL overrides; enhance.py reads the same env
COMFY_OUTPUT = enhance.COMFY_ROOT / 'output'
CLIENT_ID = 'freevideo-director'

PRESETS = {
    'draft': {'label': 'Draft', 'two_pass': True, 'finish': False},
    'final': {'label': 'Final', 'two_pass': False, 'finish': True},
}
STAGES = {
    'render': 'Render',
    'upscale': 'Upscale to 1080p',
    'smooth': 'Smooth to 48 fps',
    'encode': 'Encode',
}
# enhance.graph() node ids -> stage
ENHANCE_NODES = {'1': 'upscale', '2': 'upscale', '3': 'upscale', '4': 'upscale', '5': 'smooth', '6': 'encode'}


# ---------------------------------------------------------------- ComfyUI REST

def comfy(path, data=None, headers=None):
    req = urllib.request.Request(COMFY + path, data, headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def submit_prompt(payload):
    """POST /prompt exactly once. An explicit ComfyUI refusal fails the job; a dropped or
    unparseable answer is ambiguous -- resubmitting could render the take twice, so callers
    must treat it as unknown (raises AmbiguousSubmit)."""
    body = json.dumps({**payload, 'client_id': CLIENT_ID}).encode()
    try:
        return comfy('/prompt', body, {'Content-Type': 'application/json'})['prompt_id']
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'ComfyUI rejected the job: {e.read().decode(errors="replace")[:800]}')
    except (OSError, ValueError, KeyError) as e:
        raise AmbiguousSubmit(str(e) or type(e).__name__)


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


def build_graph(prompt, seconds, width, height, seed, refs, first, last, two_pass=True, loras=None):
    """Build the exact FreeVideo workflow for `prompt` (refs are <Picture N> files; first/last are
    frames), upload the pictures, and return the dict WITHOUT submitting it: the caller records the
    graph in the job's snapshot first, then submits it as-is. `loras` is a [{name, strength, enabled}]
    list; disabled rows ride along in the node so the graph matches the project."""
    gen = {'text': prompt, 'width': width, 'height': height, 'seconds': float(seconds), 'seed': seed,
           'two_pass': two_pass, 'base_steps': 8, 'refine_steps': 2, 'force_regenerate': False}
    wf, n = {}, 0
    def node(class_type, inputs):
        nonlocal n
        n += 1
        wf[str(n)] = {'class_type': class_type, 'inputs': inputs}
        return str(n)
    def load(name, raw):
        return node('LoadImage', {'image': upload(name, raw)})
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


def ws_listen():
    """Minimal RFC 6455 client: keep PROGRESS updated from ComfyUI's /ws for our client id."""
    while True:
        try:
            s = socket.create_connection(('127.0.0.1', 8190), timeout=None)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f'GET /ws?clientId={CLIENT_ID} HTTP/1.1\r\nHost: 127.0.0.1:8190\r\nUpgrade: websocket\r\n'
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

JOB_TIMEOUT = float(os.environ.get('JOB_TIMEOUT') or 4 * 3600)   # wall-clock cap per job
POLL_SECONDS = 1.5                                              # worker poll sleep

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


def reserve(job, keys, reuse=False):
    """Claim `keys` for `job` (all-or-nothing). Refuse while another live job holds one:
    return that job when `reuse`, else raise Busy. Replaces the unlocked SCENE_JOB write."""
    with RESERVE_LOCK:
        for key in keys:
            owner = live_owner(RESERVES.get(key))
            if owner and owner['id'] != job['id']:
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
                if key[0] == 'scene' and key[1] == slug and live_owner(RESERVES[key])}


def live_jobs(slug=None):
    with LOCK:
        return [j for j in JOBS.values()
                if not j['done'] and (slug is None or j.get('project') == slug)]


def abandon(job):
    """Drop a never-started job (its endpoint refuses the work); releases nothing it held."""
    with LOCK:
        if JOBS.get(job['id']) is job and not job['done']:
            del JOBS[job['id']]
    job['done'] = True


def transition(job, to):
    """Every state change goes through here; illegal moves are programmer errors."""
    with LOCK:
        cur = job['state']
        if to not in TRANSITIONS.get(cur, set()):
            raise AssertionError(f'illegal job transition {cur} -> {to}')
        job['state'] = to
        if to in TERMINAL:
            job['done'] = True


def check_cancel(job):
    if job['cancel']:
        raise Cancelled()


def new_job(kind, stage_keys, project=None, scene=None):
    job = {'id': uuid.uuid4().hex[:12], 'kind': kind, 'created': time.time(), 'error': None, 'done': False,
           'state': 'queued', 'deadline': time.time() + JOB_TIMEOUT,
           'stages': [{'key': k, 'label': STAGES[k], 'state': 'pending', 'started': None, 'ended': None,
                       'value': 0, 'max': 0} for k in stage_keys],
           'take': None, 'final': None, 'prompt_ids': [], 'cancel': False,
           'project': project, 'scene': str(scene) if scene is not None else None, 'holds': []}
    with LOCK:
        JOBS[job['id']] = job
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
    state and its reservations are released."""
    try:
        transition(job, 'preparing')
        check_cancel(job)
        fn(job, *args, **kwargs)
        if not job['done']:
            finish(job)
    except Cancelled:
        cancel_job(job)
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
        release(job)


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
    pid = submit_prompt({'prompt': graph})
    job['prompt_ids'].append(pid)
    transition(job, 'submitted')
    transition(job, 'running')

    def progress():
        p = PROGRESS.get(pid, {})
        st.update(value=p.get('value', 0), max=p.get('max', 0))

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
    """SeedVR2 -> RIFE -> encode for `run`; `job` must already hold what it may touch."""
    activate(job, 'upscale')
    check_cancel(job)                                  # a cancel after prep never submits
    run_dir = enhance.run_dir_of(run)
    workdir = enhance.new_workdir(run_dir, job['id'])   # private to this job; two enhances never share a name
    try:
        source, _ = enhance.intermediate(run_dir, workdir)
        enhance.free_gpu()
        pid = submit_prompt({'prompt': enhance.graph(source, run_dir.name)})
        job['prompt_ids'].append(pid)
        if job['state'] == 'preparing':                # enhance-only job; a render chain is already running
            transition(job, 'submitted')
            transition(job, 'running')
        if job['take'] is None:
            job['take'] = {'path': str(run_dir / 'video.mp4'), 'run': str(run_dir), 'url': None}

        def progress():
            p = PROGRESS.get(pid, {})
            if p.get('node') in ENHANCE_NODES:
                st = activate(job, ENHANCE_NODES[p['node']])
                st.update(value=p.get('value', 0), max=p.get('max', 0))

        final = poll(job, lambda: enhance.result(pid), progress)
    finally:
        enhance.remove_workdir(workdir)   # the lossless raw mkv is only needed while ComfyUI reads it
    job['final'] = final
    if on_final:
        on_final(final)


def job_finishing():
    """The finishing settings an enhance runs with: enhance's defaults at film fps. Every record that
    triggers an enhance snapshots these; the enhance graph itself is ephemeral (temp file paths) and
    rebuilds from them plus the take."""
    return dict(enhance.DEFAULTS, fps=enhance.FPS)


def write_enhance_snapshot(job, run):
    """Record an enhance request for a project take BEFORE submitting it: the finishing settings plus
    what was known about the source take. Ad-hoc enhances have no project and are not recorded."""
    slug = job['project']
    scene_id, take = projects.take_for_run(projects.load(slug), run)
    projects.write_job_snapshot(slug, {
        'id': job['id'], 'kind': 'enhance', 'created': job['created'], 'project': slug,
        'scene': job.get('scene') or scene_id,
        'source': {'take': take and take.get('id'), 'run': run,
                   'provenance': projects.take_provenance(take) if take else 'unknown'},
        'finishing': job_finishing()})


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


def enhance_job(job, run, on_final=None, keys=()):
    """Thread body for a standalone enhance: hold the take (plus the endpoint's scene key), record the
    finishing request against the project's take, then finish it. An ad-hoc enhance records nothing."""
    reserve(job, [take_key(run), *keys])
    if job.get('project'):
        write_enhance_snapshot(job, run)
    enhance_stages(job, run, on_final)
    if job.get('project'):
        write_job_result(job)


def public(job):
    now = time.time()
    out = {k: job[k] for k in ('id', 'kind', 'error', 'done', 'state', 'take', 'final')}
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


def scene_stage_keys(kind):
    return {'render-draft': ['render'], 'render-final': ['render', 'upscale', 'smooth', 'encode'],
            'enhance': ['upscale', 'smooth', 'encode']}[kind]


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
        'finishing': job_finishing() if PRESETS[preset_name]['finish'] else None}


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
    snapshot = render_snapshot(job, slug, scene_id, take_id, preset_name, variant,
                               args, graph, model, continuation(p, i))
    projects.write_job_snapshot(slug, snapshot)
    source_take = source_take_of(snapshot)     # a continuation names the take it started from
    def on_take(take):
        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            s['takes'].append({'id': take_id, 'job': job['id'], 'provenance': 'recorded',
                               'run': take['run'], 'path': take['path'], 'url': take['url'], 'preset': preset_name,
                               'seed': scene['seed'], 'created': time.time(), 'final': None, 'final_url': None,
                               'variant': list(variant) if variant else None,
                               'source_take': source_take})
        projects.mutate(slug, apply)
    do_render(job, graph, preset['finish'], on_take, final_recorder(slug, scene_id, lambda: job['take']['run']))
    write_job_result(job, take_id)


def final_recorder(slug, scene_id, run_of):
    def on_final(final):
        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            for t in s['takes']:
                if t['run'] == run_of():
                    t['final'], t['final_url'] = final['path'], final['url']
        projects.mutate(slug, apply)
    return on_final


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
    try:
        queue_state = comfy('/queue')
    except (OSError, ValueError):
        return
    header = {'Content-Type': 'application/json'}
    if any(item[1] in ours for item in queue_state.get('queue_running', [])):
        try:
            comfy('/interrupt', b'{}', header)
        except (OSError, ValueError):         # ComfyUI answers /interrupt with an empty body
            pass
    pending = [item[1] for item in queue_state.get('queue_pending', []) if item[1] in ours]
    if pending:
        try:
            comfy('/queue', json.dumps({'delete': pending}).encode(), header)
        except (OSError, ValueError):
            pass


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
        state['of'] = len(todo)
        for n, sid in enumerate(todo, 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            kind = 'render-' + preset_name
            job = new_job(kind, scene_stage_keys(kind), project=slug, scene=sid)
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
        for n, (sid, take) in enumerate(todo, 1):
            if state.get('cancel'):
                raise RuntimeError('cancelled')
            job = new_job('enhance', scene_stage_keys('enhance'), project=slug, scene=sid)
            state.update(step=n, scene=sid, job=job['id'])
            run_job(job, enhance_job, take['run'], final_recorder(slug, sid, lambda run=take['run']: run))
            if job['error']:
                raise RuntimeError(f'scene {n}: {job["error"]}')
    return start_batch(slug, 'Enhancing ★ takes', 0, worker)


def batch_assemble(slug, kind):
    def worker(state):
        state['of'] = 1
        state['step'] = 1
        out = projects.assemble(projects.load(slug), kind)
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
        for t in s.get('takes') or []:          # derived: legacy takes never grow stored keys
            t['provenance'] = projects.take_provenance(t)
            t['selected'] = t is c
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

def library_view(c):
    """Library reply row: the stored fields plus the URL for the character's picture."""
    return dict(c, image_url=f'/library-files/{c["slug"]}/{c["image"]}' if c.get('image') else None)


def queue_view():
    """Everything that is running now: jobs (with learned estimates) and batches, oldest first."""
    now = time.time()
    def film(slug, scene_id=None):
        try:
            p = projects.load(slug)
        except Exception:
            return None, None
        scene = next((s for s in p['scenes'] if str(s['id']) == scene_id), None)
        return p.get('name') or slug, scene['title'] if scene else None
    jobs = []
    for jid, job in list(JOBS.items()):
        if job['done']:
            continue
        slug, scene_id = job.get('project'), job.get('scene')
        name, title = film(slug, scene_id) if slug else (None, None)
        elapsed = round(now - job['created'])
        estimate = stats.estimate(job['kind'])
        estimate = round(estimate) if estimate is not None else None
        stage = next((s['label'] for s in job['stages'] if s['state'] == 'active'), None)
        jobs.append({'id': jid, 'kind': job['kind'], 'state': job['state'], 'project': slug,
                     'project_name': name, 'scene': scene_id, 'scene_title': title, 'elapsed': elapsed,
                     'estimate': estimate, 'eta': max(0, estimate - elapsed) if estimate is not None else None,
                     'stage': stage})
    batches = []
    for slug, b in list(BATCH.items()):
        if not b['done']:
            batches.append({'project': slug, 'project_name': film(slug)[0], 'label': b['label'], 'step': b['step'],
                            'of': b['of'], 'scene': b['scene'], 'seconds': round(now - b['started'])})
    return {'jobs': jobs, 'batches': batches}


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
        if self.path == '/api/queue':
            return self.reply(200, queue_view())
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
                loras = clean_loras(data.get('loras'))
                job = new_job(data.get('preset', 'draft'),
                              ['render', 'upscale', 'smooth', 'encode'] if preset['finish'] else ['render'])
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
            self.reply(404, {'error': 'not found'})
        except (director.DirectorError, projects.ProjectError, library.LibraryError) as e:
            self.reply(400, {'error': str(e)})
        except Busy as e:
            self.reply(409, {'error': str(e)})                # reserved scene/take, or a refused prerequisite
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
            write_scene_prompt(slug, data['scene'])
            return self.reply(200, project_view(slug))
        if action == 'prompts':
            batch_prompts(slug, bool(data.get('only_missing', True)))
            return self.reply(200, project_view(slug))
        if action == 'render':                         # one scene
            preset = data.get('preset', 'draft')
            if preset not in PRESETS:
                raise projects.ProjectError('unknown preset')
            p = projects.load(slug)
            i = projects.scene_index(p, data['scene'])   # unknown scene -> 400 before anything is accepted
            validate_scene_media(p, i)
            kind = 'render-' + preset
            job = new_job(kind, scene_stage_keys(kind), project=slug, scene=data['scene'])
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
            run = data.get('run') or (projects.chosen(scene) or {}).get('run')
            if not run:
                raise projects.ProjectError('this scene has no take yet')
            enhance.run_dir_of(run)                    # fail fast if it is not a FreeVideo take
            job = new_job('enhance', scene_stage_keys('enhance'), project=slug, scene=data['scene'])
            holder = reserve(job, [take_key(run), scene_key(slug, data['scene'])], reuse=True)
            if holder:                                 # duplicate click: the live job already shows on the scene
                abandon(job)
                return self.reply(200, project_view(slug))
            threading.Thread(target=run_job,
                             args=(job, enhance_job, run, final_recorder(slug, data['scene'], lambda: run),
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
            n = data.get('n')
            if type(n) is not int or not 2 <= n <= 6:
                raise projects.ProjectError('n must be an integer from 2 to 6')
            projects.scene_index(projects.load(slug), data['scene'])   # validate before anything starts
            batch_variants(slug, data['scene'], n)
            return self.reply(200, project_view(slug))
        if action == 'draft-all':
            batch_render(slug, 'draft', bool(data.get('only_missing', True)))
            return self.reply(200, project_view(slug))
        if action == 'enhance-starred':
            batch_enhance(slug)
            return self.reply(200, project_view(slug))
        if action == 'assemble':
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
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8192)
    p.add_argument('--allow-host', action='append', default=[], metavar='NAME',
                   help='extra host name to admit (repeatable)')
    a = p.parse_args()
    Handler.extra_hosts = set(a.allow_host)
    projects.recover_interrupted()
    threading.Thread(target=ws_listen, daemon=True).start()
    print(f'FreeVideo director UI: http://{a.host}:{a.port}')
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
