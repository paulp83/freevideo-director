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
COMFY = 'http://127.0.0.1:8190'
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


def upload(name, raw):
    """Upload image bytes to ComfyUI's input folder; return the stored name."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\ndirector\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
    r = comfy('/upload/image', body, {'Content-Type': f'multipart/form-data; boundary={boundary}'})
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


def queue(prompt, seconds, width, height, seed, refs, first, last, two_pass=True, loras=None):
    """Build the FreeVideo graph for the mode and queue it; return the ComfyUI prompt id."""
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
    body = json.dumps({'prompt': wf, 'client_id': CLIENT_ID}).encode()
    return comfy('/prompt', body, {'Content-Type': 'application/json'})['prompt_id']


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

JOBS = {}
LOCK = threading.Lock()


def new_job(kind, stage_keys):
    job = {'id': uuid.uuid4().hex[:12], 'kind': kind, 'created': time.time(), 'error': None, 'done': False,
           'stages': [{'key': k, 'label': STAGES[k], 'state': 'pending', 'started': None, 'ended': None,
                       'value': 0, 'max': 0} for k in stage_keys],
           'take': None, 'final': None, 'prompt_ids': [], 'cancel': False}
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
    already = job['done']
    now = time.time()
    for s in job['stages']:
        if s['state'] in ('pending', 'active'):
            s.update(state='done', ended=now, started=s['started'] or now)
    job['done'] = True
    if not already:                                # learn how long this kind of job takes (never breaks a job)
        try:
            stats.record(job['kind'], now - job['created'])
        except Exception:
            pass


def fail(job, error):
    for s in job['stages']:
        if s['state'] == 'active':
            s.update(state='error', ended=time.time())
    job['error'] = str(error)
    job['done'] = True


def run_render(job, args, finish_after, on_take=None, on_final=None):
    try:
        st = activate(job, 'render')
        pid = queue(**args)
        job['prompt_ids'].append(pid)
        while (take := render_result(pid)) is None:
            if job['cancel']:
                raise RuntimeError('cancelled')
            p = PROGRESS.get(pid, {})
            st.update(value=p.get('value', 0), max=p.get('max', 0))
            time.sleep(1.5)
        job['take'] = take
        if on_take:
            on_take(take)
        if finish_after:
            run_enhance(job, take['run'], on_final)
        else:
            finish(job)
    except Exception as e:      # surfaced in the UI, never kills the server
        fail(job, e)


def run_enhance(job, run, on_final=None):
    try:
        activate(job, 'upscale')
        pid, run_dir = enhance.submit(run)
        job['prompt_ids'].append(pid)
        if job['take'] is None:
            job['take'] = {'path': str(run_dir / 'video.mp4'), 'run': str(run_dir), 'url': None}
        while (final := enhance.result(pid)) is None:
            if job['cancel']:
                raise RuntimeError('cancelled')
            p = PROGRESS.get(pid, {})
            if p.get('node') in ENHANCE_NODES:
                st = activate(job, ENHANCE_NODES[p['node']])
                st.update(value=p.get('value', 0), max=p.get('max', 0))
            time.sleep(1.5)
        job['final'] = final
        if on_final:
            on_final(final)
        finish(job)
    except Exception as e:
        fail(job, e)
    finally:
        enhance.cleanup_intermediate(run)         # the lossless -raw.mkv is only needed while ComfyUI reads it


def public(job):
    now = time.time()
    out = {k: job[k] for k in ('id', 'kind', 'error', 'done', 'take', 'final')}
    out['stages'] = [{**s, 'seconds': round((s['ended'] or now) - s['started']) if s['started'] else None}
                     for s in job['stages']]
    out['elapsed'] = round(now - job['created'])
    return out


def decode(image):
    """{'name':..., 'data': 'data:...;base64,...'} -> (name, bytes)."""
    return image['name'], base64.b64decode(image['data'].split(',', 1)[-1])


# ---------------------------------------------------------------- film projects

BATCH = {}        # slug -> {'label', 'step', 'of', 'scene', 'job', 'error', 'done', 'started'}
SCENE_JOB = {}    # (slug, scene id) -> job id of the scene's latest render / enhance / prompt


def _files(paths):
    return [(Path(f).name, Path(f).read_bytes()) for f in paths]


def project_busy(slug):
    """True while a batch or any registered job of this film is still running."""
    b = BATCH.get(slug)
    if b and not b['done']:
        return True
    return any(s == slug and jid in JOBS and not JOBS[jid]['done'] for (s, _), jid in SCENE_JOB.items())


def check_of(report):
    return {'retried': report['retried'], 'first_failures': report['first_failures'], 'final_failures': report['final_failures']}


def write_scene_prompt(slug, scene_id, think=True):
    """Director writes scene's H3 prompt from the bible + action (+ refs or the previous last frame)."""
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
    return mode


def render_scene(slug, scene_id, preset_name, job, variant=None):
    """Blocking: render one scene (Draft or Final) and record the take (and final) in the project."""
    preset = PRESETS[preset_name]
    p = projects.load(slug)
    i = projects.scene_index(p, scene_id)
    scene = p['scenes'][i]
    if not scene['prompt'].strip():
        write_scene_prompt(slug, scene_id)
        p = projects.load(slug)
        scene = p['scenes'][i]
    refs, first = projects.scene_media(p, i)
    prompt = projects.ensure_i2va(scene['prompt']) if first else scene['prompt']
    w, h = p['size']
    args = dict(prompt=prompt, seconds=scene['seconds'], width=w, height=h, seed=scene['seed'],
                refs=_files(refs), first=_files([first])[0] if first else None, last=None,
                two_pass=preset['two_pass'], loras=p.get('loras'))
    def on_take(take):
        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            s['takes'].append({'run': take['run'], 'path': take['path'], 'url': take['url'], 'preset': preset_name,
                               'seed': scene['seed'], 'created': time.time(), 'final': None, 'final_url': None,
                               'variant': list(variant) if variant else None})
        projects.mutate(slug, apply)
    run_render(job, args, preset['finish'], on_take, final_recorder(slug, scene_id, lambda: job['take']['run']))


def final_recorder(slug, scene_id, run_of):
    def on_final(final):
        def apply(proj):
            s = proj['scenes'][projects.scene_index(proj, scene_id)]
            for t in s['takes']:
                if t['run'] == run_of():
                    t['final'], t['final_url'] = final['path'], final['url']
        projects.mutate(slug, apply)
    return on_final


def scene_job(slug, scene_id, kind):
    keys = {'render-draft': ['render'], 'render-final': ['render', 'upscale', 'smooth', 'encode'],
            'enhance': ['upscale', 'smooth', 'encode']}[kind]
    job = new_job(kind, keys)
    SCENE_JOB[(slug, scene_id)] = job['id']
    return job


def start_batch(slug, label, steps, worker):
    """Run worker(step_callback) in a thread; one batch per project at a time."""
    current = BATCH.get(slug)
    if current and not current['done']:
        raise projects.ProjectError(f'busy: {current["label"]} is still running')
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
    for (s, _), jid in list(SCENE_JOB.items()):
        job = JOBS.get(jid)
        if s == slug and job and not job['done']:
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
            job = scene_job(slug, sid, 'render-' + preset_name)
            state.update(step=n, scene=sid, job=job['id'])
            render_scene(slug, sid, preset_name, job)
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
            job = scene_job(slug, scene_id, 'render-draft')
            state.update(step=k, scene=scene_id, job=job['id'])
            render_scene(slug, scene_id, 'draft', job, variant=(k, n))
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
            job = scene_job(slug, sid, 'enhance')
            state.update(step=n, scene=sid, job=job['id'])
            run_enhance(job, take['run'], final_recorder(slug, sid, lambda run=take['run']: run))
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
    b = BATCH.get(slug)
    p['batch'] = dict(b, seconds=round(time.time() - b['started'])) if b else None
    p['batch_job'] = public(JOBS[b['job']]) if b and b.get('job') in JOBS else None
    return p

def library_view(c):
    """Library reply row: the stored fields plus the URL for the character's picture."""
    return dict(c, image_url=f'/library-files/{c["slug"]}/{c["image"]}' if c.get('image') else None)


def queue_view():
    """Everything that is running now: jobs (with learned estimates) and batches, oldest first."""
    now = time.time()
    where = {jid: key for key, jid in SCENE_JOB.items()}          # job id -> (project slug, scene id)
    def film(slug, scene_id=None):
        try:
            p = projects.load(slug)
        except Exception:
            return None, None
        scene = next((s for s in p['scenes'] if s['id'] == scene_id), None)
        return p.get('name') or slug, scene['title'] if scene else None
    jobs = []
    for jid, job in list(JOBS.items()):
        if job['done']:
            continue
        slug, scene_id = where.get(jid, (None, None))
        name, title = film(slug, scene_id) if slug else (None, None)
        elapsed = round(now - job['created'])
        estimate = stats.estimate(job['kind'])
        estimate = round(estimate) if estimate is not None else None
        stage = next((s['label'] for s in job['stages'] if s['state'] == 'active'), None)
        jobs.append({'id': jid, 'kind': job['kind'], 'project': slug, 'project_name': name, 'scene': scene_id,
                     'scene_title': title, 'elapsed': elapsed, 'estimate': estimate,
                     'eta': max(0, estimate - elapsed) if estimate is not None else None, 'stage': stage})
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
                names = comfy('/object_info/FreeVideoLoRA')['FreeVideoLoRA']['input']['required']['lora'][0]
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
                return self.reply(200, project_view(self.path.split('/')[3]))
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
                threading.Thread(target=run_render, args=(job, args, preset['finish']), daemon=True).start()
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
                job = new_job('enhance', ['upscale', 'smooth', 'encode'])
                threading.Thread(target=run_enhance, args=(job, run), daemon=True).start()
                return self.reply(200, public(job))
            self.reply(404, {'error': 'not found'})
        except (director.DirectorError, projects.ProjectError, library.LibraryError) as e:
            self.reply(400, {'error': str(e)})
        except (KeyError, ValueError, TypeError) as e:
            self.reply(400, {'error': f'bad request: {e}'})
        except FileNotFoundError as e:
            self.reply(400, {'error': str(e)})
        except urllib.error.HTTPError as e:
            self.reply(502, {'error': f'ComfyUI rejected the job: {e.read().decode()[:800]}'})
        except OSError as e:
            self.reply(502, {'error': f'ComfyUI unreachable at {COMFY}: {e}'})

    def project_action(self, slug, action, data):
        if action == 'save':
            return self.reply(200, projects.mutate(slug, lambda p: projects.update_from_editor(p, data)))
        if action == 'storyboard':                     # re-plan: replaces scenes (the page confirms first)
            p = projects.load(slug)
            plan, _ = director.storyboard(data.get('logline') or p['logline'], p['seconds'],
                                          projects.ref_files(p), p['aspect'], character_notes=p.get('character_notes', ''))
            def apply(proj):
                proj['logline'] = data.get('logline') or proj['logline']
                projects.apply_storyboard(proj, plan)
            return self.reply(200, projects.mutate(slug, apply))
        if action == 'prompt':                         # one scene, synchronous (~15 s)
            write_scene_prompt(slug, data['scene'])
            return self.reply(200, project_view(slug))
        if action == 'prompts':
            batch_prompts(slug, bool(data.get('only_missing', True)))
            return self.reply(200, project_view(slug))
        if action == 'render':                         # one scene
            preset = data.get('preset', 'draft')
            if preset not in PRESETS:
                raise projects.ProjectError('unknown preset')
            job = scene_job(slug, data['scene'], 'render-' + preset)
            threading.Thread(target=render_scene, args=(slug, data['scene'], preset, job), daemon=True).start()
            return self.reply(200, project_view(slug))
        if action == 'enhance':                        # one scene's take
            p = projects.load(slug)
            scene = p['scenes'][projects.scene_index(p, data['scene'])]
            run = data.get('run') or (projects.chosen(scene) or {}).get('run')
            if not run:
                raise projects.ProjectError('this scene has no take yet')
            job = scene_job(slug, data['scene'], 'enhance')
            threading.Thread(target=run_enhance, args=(job, run, final_recorder(slug, data['scene'], lambda: run)),
                             daemon=True).start()
            return self.reply(200, project_view(slug))
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
        if action == 'star':
            def apply(p):
                s = p['scenes'][projects.scene_index(p, data['scene'])]
                s['star'] = data.get('run')
            projects.mutate(slug, apply)
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
