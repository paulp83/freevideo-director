#!/usr/bin/env python3
"""Controllable stub ComfyUI, run as its own OS process for the restart-acceptance suite.

The director under test is SIGKILLed and restarted against a backend that keeps state the
director cannot see, so the stub lives outside the director's process. It speaks just enough
of the ComfyUI HTTP API for web.py: POST /prompt (echoing the client-supplied prompt_id like
the real ComfyUI), GET/POST /queue, GET /history/<id>, POST /interrupt, POST /free,
POST /upload/image and GET /view. Output files a completed prompt "produced" are written
under --root/output so the director's run paths resolve on disk.

A private /_ctl control channel drives it: set the mode, hold or release a submit, complete
or fail a prompt on command, and report every /prompt body it ever received plus its current
queue/history state. Modes for POST /prompt:

  auto         accept; land completed in history at once (output file written)
  run          accept; show as running
  queue        accept; leave pending
  gone         accept, but never appear in queue or history
  fail         accept; land failed in history at once
  hold         record the body, never accept: the response blocks until /_ctl/release
  run-noresp   accept as running, then block the HTTP response (the acceptance window)

and /_ctl/hold-uploads blocks /upload/image responses the same way, which is how a kill
during preparation (before any submit) is made deterministic. Stdlib only; test scaffolding,
never part of the shipped app.
"""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import enhance                                  # the real finishing graph names the node
                                                # classes and model files this stub should offer


RENDER_CLASSES = ('LoadImage', 'FreeVideoReference', 'FreeVideoLoRAStack', 'FreeVideoGenerate')


DROP = ()


def object_info(without=None):
    """What a fully installed ComfyUI answers /object_info with, derived from the graphs the
    director really submits: enhance.graph() for the finishing classes and model file names,
    the render class names below. `without` drops classes, for negative readiness tests."""
    if without is None:
        without = DROP
    info = {}
    for node in enhance.graph('probe.mp4', 'probe').values():
        required = {key: ['COMBO', {'options': [value]}]
                    for key, value in node['inputs'].items()
                    if isinstance(value, str) and value.endswith(('.safetensors', '.pth', '.pt'))}
        info[node['class_type']] = {'input': {'required': required}}
    for cls in RENDER_CLASSES:
        info.setdefault(cls, {'input': {'required': {}}})
    info['FreeVideoLoRA'] = {'input': {'required': {
        'lora': ['COMBO', {'options': ['None', 'meadow-style.safetensors',
                                       'gate-grain.safetensors']}]}}}
    return {cls: entry for cls, entry in info.items() if cls not in without}


ROOT = Path('.')                       # set from --root; "output files" land under ROOT/output
FAIL_MESSAGE = 'CUDA out of memory'   # what mode 'fail' blames

LOCK = threading.Lock()
prompt_event = threading.Event()       # gates held /prompt responses
prompt_into = 'run'                    # where a released held submit lands
upload_event = threading.Event()
upload_event.set()

STATE = {'mode': 'auto', 'bodies': [], 'accepted': [], 'pending': [], 'running': [],
         'history': {}, 'uploads': 0, 'interrupts': 0, 'seq': 0, 'releases': 0}


def _completed_entry(sub, fn):
    return {'status': {'status_str': 'success', 'completed': True},
            'outputs': {'9': {'gifs': [{'filename': fn, 'subfolder': sub, 'type': 'output'}]}}}


def _failed_entry(message):
    return {'status': {'status_str': 'error', 'completed': False,
                       'messages': [['execution_error', {'exception_message': message}]]},
            'outputs': {}}


def _land_output(seq):
    """Give a completed prompt an output the director can find: a take dir with video.mp4."""
    sub, fn = f'director/stub-take-{seq}', 'video.mp4'
    out = ROOT / 'output' / sub
    out.mkdir(parents=True, exist_ok=True)
    (out / 'video.artifacts').mkdir(exist_ok=True)      # the mp4 fallback needs the dir marker
    (out / fn).write_bytes(b'\x00\x00\x00\x20ftypmp42 stub take bytes')
    return sub, fn


def _land(pid, pid_seq, outcome, message=None):
    """Move a prompt out of the queues and into history with a verdict."""
    for q in (STATE['pending'], STATE['running']):
        if pid in q:
            q.remove(pid)
    if outcome == 'completed':
        sub, fn = _land_output(pid_seq)
        STATE['history'][pid] = _completed_entry(sub, fn)
    else:
        STATE['history'][pid] = _failed_entry(message or FAIL_MESSAGE)


class Stub(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass                          # the director died while we were answering

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw or b'{}')
        except ValueError:
            return {'_raw': raw.decode(errors='replace')}

    # -- what a complete install offers (readiness.py's finishing probe reads this) ------
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/object_info':
            return self._json(200, object_info())
        if path.startswith('/object_info/'):
            cls = path[len('/object_info/'):]
            info = object_info()
            return self._json(200, {cls: info[cls]} if cls in info else {})
    # -- prompts ----------------------------------------------------------------
    def _prompt(self, payload):
        with LOCK:
            STATE['seq'] += 1
            STATE['bodies'].append(payload)
            pid = payload.get('prompt_id') or f'stub-{STATE["seq"]}'
            STATE['accepted'].append(pid)
            mode = STATE['mode']
        if mode == 'hold':
            prompt_event.wait(120)        # the director is meant to die before this releases
            with LOCK:
                STATE['running'].append(pid)
                STATE['releases'] += 1
        elif mode == 'run-noresp':
            with LOCK:
                STATE['running'].append(pid)
            prompt_event.wait(120)        # accepted, but the answer never arrives
        elif mode == 'run':
            with LOCK:
                STATE['running'].append(pid)
        elif mode == 'queue':
            with LOCK:
                STATE['pending'].append(pid)
        elif mode in ('auto', 'fail'):
            with LOCK:
                _land(pid, STATE['seq'], 'completed' if mode == 'auto' else 'failed')
        # 'gone': accepted; then it never appears in the queue or the history at all
        self._json(200, {'prompt_id': pid, 'number': STATE['seq'], 'node_errors': {}})

    def do_POST(self):
        path, data = self.path.split('?', 1)[0], None
        if path.startswith('/_ctl/'):
            data = self._body()
            cmd = path[len('/_ctl/'):]
            if cmd == 'mode':
                with LOCK:
                    STATE['mode'] = data.get('mode', 'auto')
                prompt_event.clear()
                self._json(200, {'mode': STATE['mode']})
            elif cmd == 'release':
                global prompt_into
                prompt_into = data.get('into', 'run')
                prompt_event.set()
                self._json(200, {'released': True})
            elif cmd == 'hold-uploads':
                upload_event.clear()
                self._json(200, {'held': True})
            elif cmd == 'release-uploads':
                upload_event.set()
                self._json(200, {'held': False})
            elif cmd == 'complete':
                with LOCK:
                    pid = data['prompt_id']
                    _land(pid, STATE['accepted'].index(pid) + 1 if pid in STATE['accepted'] else 0,
                          'completed')
                self._json(200, {'completed': pid})
            elif cmd == 'fail':
                with LOCK:
                    pid = data['prompt_id']
                    _land(pid, 0, 'failed', data.get('message'))
                self._json(200, {'failed': pid})
            elif cmd == 'reset':
                with LOCK:
                    STATE.update(mode='auto', bodies=[], accepted=[], pending=[], running=[],
                                 history={}, uploads=0, interrupts=0, seq=0, releases=0)
                prompt_event.clear()
                upload_event.set()
                self._json(200, {'reset': True})
            else:
                self._json(404, {'error': f'unknown ctl {cmd}'})
            return
        if path == '/prompt':
            return self._prompt(self._body())
        if path == '/queue':
            body = self._body()
            with LOCK:
                delete = body.get('delete') if isinstance(body, dict) else None
                if delete == 'all':
                    STATE['pending'].clear()
                else:
                    for pid in delete or []:
                        if pid in STATE['pending']:
                            STATE['pending'].remove(pid)
            return self._json(200, {})
        if path == '/interrupt':
            with LOCK:
                STATE['running'].clear()
                STATE['interrupts'] += 1
            return self._json(200, {})
        if path == '/free':
            return self._json(200, {})
        if path == '/upload/image':
            with LOCK:
                STATE['uploads'] += 1                   # counted on arrival, before any hold
            upload_event.wait(120)
            return self._json(200, {'name': f'upload-{STATE["uploads"]}.png',
                                    'subfolder': 'director', 'type': 'input'})
        self._json(404, {'error': f'unknown POST {path}'})

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/_ctl/state':
            with LOCK:
                return self._json(200, {
                    'mode': STATE['mode'], 'prompt_bodies': STATE['bodies'],
                    'accepted': list(STATE['accepted']), 'pending': list(STATE['pending']),
                    'running': list(STATE['running']),
                    'history': {pid: ('completed' if e.get('status', {}).get('completed') else 'failed')
                                for pid, e in STATE['history'].items()},
                    'uploads': STATE['uploads'], 'interrupts': STATE['interrupts'],
                    'releases': STATE['releases']})
        if path == '/queue':
            with LOCK:
                rows = lambda ids: [[i, pid, [], {}, ''] for i, pid in enumerate(ids)]
                return self._json(200, {'queue_running': rows(STATE['running']),
                                        'queue_pending': rows(STATE['pending'])})
        if path.startswith('/history'):
            pid = path.rsplit('/', 1)[-1] if path != '/history' else None
            with LOCK:
                if pid and pid in STATE['history']:
                    return self._json(200, {pid: STATE['history'][pid]})
                return self._json(200, {})        # real ComfyUI answers {} for an unknown id
        if path == '/object_info':
            return self._json(200, object_info())
        if path.startswith('/object_info/'):
            cls = path[len('/object_info/'):]
            entry = object_info().get(cls)
            if entry is None:
                return self._json(404, {'error': f'no such node class {cls}'})
            return self._json(200, {cls: entry})
        if path == '/view':
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            rel = Path(q.get('subfolder', [''])[0]) / q.get('filename', [''])[0]
            f = (ROOT / 'output' / rel) if q.get('filename') else None
            if f and f.is_file():
                raw = f.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return self._json(404, {'error': 'not found'})
        self._json(200, {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--root', required=True, help='stand-in for the ComfyUI install dir')
    a = ap.parse_args()
    global ROOT
    ROOT = Path(a.root)
    (ROOT / 'output').mkdir(parents=True, exist_ok=True)
    (ROOT / 'input' / 'director').mkdir(parents=True, exist_ok=True)
    global DROP
    DROP = tuple(v for v in os.environ.get('COMFY_STUB_DROP', '').split(',') if v)
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Stub)
    print(f'stub listening {a.port}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
