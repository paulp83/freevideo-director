"""M3 Stage B3 UX acceptance: the whole flow a lead would click, end to end.

Runs the workspace project.html against the six-scene fixture and a STUB ComfyUI that records
every POST body (so the graph the server builds is checked against what is on disk), with real
jobs in the real in-process queue:

  * lazy contract: zero <video> outside the viewer/comparison and zero mp4/webm resource
    entries before the first user action (checked at load and again after a reload);
  * a counted <= 8-action flow at 1440x900: select scene 4, inspect a take's Details, compare
    the two takes, pick one ("Use this take" -> explicit selection on disk + viewer update),
    start the next operation, and see the queue drawer list it with honest times -- the
    document must not scroll (scrollY 0) at any step or viewport;
  * the exact POST bodies: two /upload/image then one /prompt whose FreeVideoGenerate inputs
    equal the disk scene's prompt/seed/size; the enhance run proves the /free + enhance graph
    and the video.mp4 fallback (no raw frames in the fixture); a real ffmpeg animatic batch
    shows the live batch bar;
  * playback + polling + focus: play a take, type into the prompt, open the drawer, wait two
    poll cycles -- nothing is lost;
  * a keyboard-only flow (no mouse calls): dialogs, drawer, comparison; focus is restored
    after every close and never lands on <body>;
  * a second 1920x1080 pass over the primary action;
  * a 390x844 phone pass (iframe, like browser_layout) that walks EVERY row of
    docs/ui-capability-map.md: each Selector token exists, is visible after opening its panel,
    sits inside the viewport width, and every control is >= 44 px tall (the stage caret is
    exempt by design but >= 44 wide);
  * a dark-theme session (Firefox content-override: dark) over both sizes.

Screenshots land in docs/screenshots/m3-acceptance/ (<= 6 per size, auto-compressed < 400 KB).
MOCKED: ComfyUI (stub server -- no renderer, no SeedVR2/RIFE execution); ffmpeg is gated by a
wrapper for the batch-cancel proof. REAL: web.py jobs, queue, cancel proof (the encode is really
killed), POST bodies, the disk state, ffmpeg media, Firefox events.
"""
import base64, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import project as projects   # noqa: E402
import make_fixture          # noqa: E402

WEB_PORT, DRIVER_PORT = 8209, 4463
SHOTS = HERE / 'docs' / 'screenshots' / 'm3-acceptance'
CAPMAP = HERE / 'docs' / 'ui-capability-map.md'
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(('ok   - ' if ok else 'FAIL - ') + name + (f'  [{detail}]' if detail and not ok else ''))


# ---------------------------------------------------------------- stub ComfyUI
POSTS = []                  # (path, body-str) in arrival order, guarded by LOCK
POST_LOCK = threading.Lock()
STATE = {'running': [], 'unknown': [], 'drop_next_prompt': False}   # running: prompt ids ComfyUI "has"


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n)
        with POST_LOCK:
            POSTS.append((self.path, raw.decode('latin1')))
        if self.path == '/prompt':
            body = json.loads(raw)
            pid = body.get('prompt_id') or f'stub-{len(POSTS)}'   # echo: like real ComfyUI
            if STATE['drop_next_prompt']:
                STATE['drop_next_prompt'] = False
                STATE['running'].append(pid)   # the prompt ARRIVED; only the answer was lost --
                STATE['unknown'].append(pid)   # survives later interrupts: it is still queued up there
                self.close_connection = True   # reconcile can then prove it is still executing
                self.connection.close()        # unanswered submit: the real unknown-job path
                return
            STATE['running'].append(pid)
            return self._send({'prompt_id': pid, 'node_errors': {}})
        if self.path == '/upload/image':
            fn = re.search(r'filename="([^"]+)"', raw.decode('latin1'))
            return self._send({'name': fn.group(1), 'subfolder': 'director', 'type': 'input'})
        if self.path == '/queue':               # target_cancel: delete only our prompts
            try:
                for pid in json.loads(raw).get('delete', []):
                    if pid in STATE['running']:
                        STATE['running'].remove(pid)
            except ValueError:
                pass
            return self._send({})
        if self.path in ('/interrupt',):       # interrupt stops the executing prompt: it leaves the queue
            # ...but only THAT one: a still-queued prompt (the unanswered unknown submit) keeps running
            STATE['running'] = [p for p in STATE['running'] if p in STATE.get('unknown', [])]
            return self._send({})
        return self._send({})                   # /free

    def do_GET(self):
        if self.path == '/queue':
            return self._send({'queue_running': [[i + 1, pid, None, ('0', '0', '0')]
                                                 for i, pid in enumerate(STATE['running'])],
                               'queue_pending': []})
        if self.path.startswith('/history/'):
            return self._send({})               # never finishes: the stub does not render
        if self.path.startswith('/object_info/'):
            return self._send({'FreeVideoLoRA': {'input': {'required': {
                'lora': ['COMBO', {'options': ['meadow-style.safetensors', 'gate-grain.safetensors']}]}}}})
        return self._send({})


# ---------------------------------------------------------------- driver
class Driver:
    def __init__(self, prefs=None):
        self.base = f'http://127.0.0.1:{DRIVER_PORT}'
        self.proc = subprocess.Popen(['geckodriver', '--port', str(DRIVER_PORT)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.call('GET', '/status'); break
            except Exception:
                time.sleep(0.2)
        fprefs = {'media.autoplay.blocking_policy': 0}
        fprefs.update(prefs or {})
        caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'unhandledPromptBehavior': 'ignore',
                                                'moz:firefoxOptions': {'args': ['-headless'], 'prefs': fprefs}}}}
        ses = self.call('POST', '/session', caps)
        if 'sessionId' not in ses.get('value', {}):
            raise RuntimeError('geckodriver session failed: ' + json.dumps(ses)[:300])
        self.sid = ses['value']['sessionId']

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw)
            except ValueError:
                msg = raw.decode(errors='replace')[:120].strip() or f'http {e.code}'
                return {'value': {'error': f'http {e.code}', 'message': msg}}

    def s(self, method, path, body=None):
        r = self.call(method, f'/session/{self.sid}{path}',
                      body if body is not None else ({} if method == 'POST' else None))
        if isinstance(r.get('value'), dict) and 'error' in r['value']:
            raise RuntimeError(r['value'].get('message', r['value']['error']))
        return r.get('value')

    def js(self, script, *args):
        return self.s('POST', '/execute/sync', {'script': script, 'args': list(args)})

    def go(self, url): self.s('POST', '/url', {'url': url})

    def wait(self, script, timeout=10, *args):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js('return (' + script + ')', *args)
            if v: return v
            time.sleep(0.15)
        return None

    def element(self, sel):
        el = self.s('POST', '/element', {'using': 'xpath' if sel.startswith('//') else 'css selector',
                                         'value': sel})
        return list(el.values())[0]

    def click(self, sel):
        self.s('POST', f"/element/{self.element(sel)}/click", {})

    def key(self, ch):
        self.s('POST', '/actions', {'actions': [{'type': 'key', 'id': 'k',
                                                 'actions': [{'type': 'keyDown', 'value': ch},
                                                             {'type': 'keyUp', 'value': ch}]}]})

    def frame_on(self, sel):
        # this geckodriver takes the bare W3C element reference as the frame id (probed: the
        # {frame: ...} wrapper it rejects); switching to an <iframe> element enters it
        ref = sel if isinstance(sel, dict) else \
            self.s('POST', '/element', {'using': 'css selector', 'value': sel})
        self.s('POST', '/frame', {'id': ref})
        return ref

    def frame_off(self, fid=None):
        self.s('POST', '/frame', {'id': fid if fid is not None else None})

    def resize(self, w, h):
        for _ in range(6):
            cur_w, cur_h = self.js('return [window.innerWidth, window.innerHeight]')
            if (cur_w, cur_h) == (w, h):
                return
            rect = self.s('GET', '/window/rect')
            self.s('POST', '/window/rect', {'width': rect['width'] + (w - cur_w), 'height': rect['height'] + (h - cur_h)})
            time.sleep(0.3)
        raise RuntimeError(f'cannot resize viewport to {w}x{h}')

    def screenshot(self, path):
        return self._png(f'/session/{self.sid}/screenshot', path)

    def element_screenshot(self, fid, path):
        return self._png(f'/session/{self.sid}/element/{fid}/screenshot', path)

    def _png(self, url_path, path):
        png = base64.b64decode(self.call('GET', url_path)['value'])
        path.write_bytes(png)
        if path.stat().st_size > 400 * 1024:
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path),
                            '-vf', 'palettegen', str(path) + '.pal.png'], check=True)
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path), '-i', str(path) + '.pal.png',
                            '-lavfi', 'paletteuse', str(path) + '.q.png'], check=True)
            os.replace(str(path) + '.q.png', str(path))
            os.remove(str(path) + '.pal.png')
        return path.stat().st_size

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


# the launcher mirrors web.py's __main__ startup block; the unknown-job and second-film rows
# are registered through the real job API before the server starts answering. enhance's
# ComfyUI root points inside the temp dir so the real fallback/conversion code never touches
# the machine's ../ComfyUI.
LAUNCHER = """
import sys, json, threading
sys.path.insert(0, %r); sys.path.insert(0, %r)
import project
from pathlib import Path
project.ROOT = Path(%r)
import web, enhance, library
from http.server import ThreadingHTTPServer
enhance.COMFY_ROOT = Path(%r)
library.ROOT = Path(%r)                       # isolated: the walk gets a real populated list
library.save('Hare', 'grey hare, red knitted scarf, brass goggles')
slug, ids = json.loads(%r)
other = project.create('Other film', 'A different film entirely.', 20, '16:9', [])[ 'slug']
o = web.new_job("render-final", ["render"], project=other)
web.transition(o, "preparing"); web.activate(o, "render"); web.transition(o, "submitted"); web.transition(o, "running")
project.recover_interrupted()
web.sweep_enhance_tmp()
threading.Thread(target=web.reconcile_loop, daemon=True).start()
threading.Thread(target=web.ws_listen, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", %d), web.Handler).serve_forever()
"""


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def posts_after(n):
    with POST_LOCK:
        return list(POSTS[n:])


def scene_on_disk(slug, idx):
    p = projects.load(slug)
    return p['scenes'][idx], p


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-acceptance-'))
    stub = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_url = f'http://127.0.0.1:{stub.server_address[1]}'
    man = make_fixture.build(tmp)
    slug, ids = man['slug'], man['scene_ids']
    launcher = LAUNCHER % (str(HERE), str(HERE / 'tests' / 'fixtures'), str(projects.ROOT),
                           str(tmp / 'comfy-output'), str(tmp / 'library'), json.dumps([slug, ids]), WEB_PORT)
    # ffmpeg gate: a wrapper shadows ffmpeg for the server only; with the flag file present it
    # execs the real binary, and the phone batch walk removes the flag to stall one encode.
    gate_dir = tmp / 'ffmpeg-gate'
    gate_dir.mkdir()
    GATE = tmp / 'ffmpeg-open'
    (gate_dir / 'ffmpeg').write_text('#!/bin/sh\nwhile [ ! -f "$M3B3_FFMPEG_GATE" ]; do sleep 0.25; done\n'
                                     'exec /usr/bin/ffmpeg "$@"\n')
    (gate_dir / 'ffmpeg').chmod(0o755)
    GATE.write_text('open')
    webp = subprocess.Popen([sys.executable, '-c', launcher],
                            env=dict(os.environ, COMFY_URL=stub_url, JOBS_DB=str(tmp / 'jobs.db'),
                                     PATH=str(gate_dir) + os.pathsep + os.environ['PATH'],
                                     M3B3_FFMPEG_GATE=str(GATE)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SHOTS.mkdir(parents=True, exist_ok=True)
    d = None
    try:
        for _ in range(80):
            try:
                get_json(f'http://127.0.0.1:{WEB_PORT}/api/projects'); break
            except Exception:
                time.sleep(0.25)

        # ================= session A: lazy contract =================
        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.resize(1440, 900)
        lazy = d.js("""const vids = [...document.querySelectorAll('video')].filter(
                          v => v.id !== 'viewer' && !(v.closest('#cmpWrap')));
                       const res = performance.getEntriesByType('resource')
                          .filter(e => /\\.(mp4|webm)(\\?|$)/.test(e.name)).map(e => e.name);
                       const strip = document.querySelector('#takeStrip');
                       return {stray: vids.length, media: res,
                               stripVids: strip ? strip.querySelectorAll('video').length : -1,
                               preload: document.getElementById('viewer').preload,
                               posters: [...document.querySelectorAll('#takeStrip img')].length};""")
        check('lazy: zero <video> outside viewer/comparison on load',
              lazy['stray'] == 0 and lazy['stripVids'] == 0, json.dumps(lazy))
        check('lazy: zero .mp4/.webm resource entries before any user action',
              lazy['media'] == [], json.dumps(lazy['media']))
        check('lazy: take strip shows posters (img), viewer preload=none',
              lazy['posters'] >= 2 and lazy['preload'] == 'none', json.dumps(lazy))
        check('load: page does not scroll at 1440x900',
              d.js('return [document.documentElement.scrollTop, document.body.scrollTop];') == [0, 0])
        check('load: dark check deferred; backend chip reachable (stub /object_info)',
              'reachable' in d.js("return document.getElementById('backend').textContent;"),
              d.js("return document.getElementById('backend').textContent;"))
        d.screenshot(SHOTS / '1-load-lazy.png')

        # ================= counted flow: <= 8 user actions =================
        actions = 0
        z = lambda: d.js('return [document.documentElement.scrollTop, document.body.scrollTop];')

        # action 1: select scene 4 (Lantern) with a real mouse click
        d.click('#navigator .navitem:nth-child(4)'); actions += 1
        st1 = d.js("""const v = document.getElementById('viewer');
                      const nav = document.querySelectorAll('#navigator .navitem');
                      return {primary: document.getElementById('bPrimary').textContent,
                              cards: document.querySelectorAll('#takeStrip .take').length,
                              src: v.getAttribute('src') || '',
                              sel: nav[3].getAttribute('aria-selected'),
                              scroll: [document.documentElement.scrollTop, document.body.scrollTop]};""")
        t2, t1 = man['takes'][3][1], man['takes'][3][0]
        check(f'flow 1: select scene 4 -> primary "Use this take", 2 take cards, newest in viewer',
              st1['primary'] == 'Use this take' and st1['cards'] == 2 and t2 in st1['src']
              and st1['sel'] == 'true', json.dumps(st1))
        check('flow 1: scrollY stays 0', st1['scroll'] == [0, 0], str(st1['scroll']))

        # action 2: open this take's Details
        d.click('#takeStrip .take:first-child [data-k="bDetails"]'); actions += 1
        tech = d.wait("""document.getElementById('techDetails').open
                         && document.querySelectorAll('#techBox .trow').length > 0
                         && (() => {const t = [...document.querySelectorAll('#techBox .trow')]
                              .map(r => r.textContent).join('\\n');
                              return t.includes('42') ? [t.slice(0, 160)] : null;})()""", timeout=6)
        check('flow 2: Details open with provenance rows naming seed 42', bool(tech), json.dumps(tech)[:220])

        # action 3: compare the two takes
        d.click('#bCompare'); actions += 1
        cmp = d.wait("""(() => {const w = document.getElementById('cmpWrap');
                        if (w.hidden) return null;
                        const btns = [...w.querySelectorAll('button')].filter(b => b.textContent === 'Use this take');
                        return {btns: btns.length, txt: w.textContent};})()""", timeout=6)
        check('flow 3: comparison open: both seeds named, in-use pane labelled, unpinned pane offers Use',
              bool(cmp) and cmp['btns'] == 1 and '42' in cmp['txt'] and '41' in cmp['txt']
              and 'in use' in cmp['txt'], json.dumps(cmp)[:200])
        d.screenshot(SHOTS / '2-compare-two-takes.png')

        # action 4: pick take 1 from its pane (real mouse click)
        d.js("""const pane = [...document.querySelectorAll('#cmpWrap .cmpPane')]
                        .find(p => /seed 41(?!\\d)/.test(p.textContent));
                const pick = [...pane.querySelectorAll('button')].find(b => b.textContent === 'Use this take');
                pick.click(); return 1;""")
        actions += 1
        time.sleep(1.2)
        sc4, _ = scene_on_disk(slug, 3)
        run1 = next(t['run'] for t in sc4['takes'] if t['id'] == t1)
        check('flow 4: Use this take -> explicit selection on disk names the older take',
              sc4['selection'] == 'explicit' and sc4['selected_take'] in (t1, run1),
              json.dumps({'selection': sc4['selection'], 'selected_take': sc4['selected_take'],
                          'want_id': t1, 'want_run': run1}))
        vinfo = d.js("""return {chosen: [...document.querySelectorAll('#takeStrip .take')]
                                  .filter(c => c.classList.contains('chosen')).map(c => c.dataset.t),
                                status: document.getElementById('scStatus').textContent};""")
        check('flow 4: the strip re-marks the pinned take as in use (viewer keeps the take under comparison)',
              vinfo['chosen'] == [t1] and 'seed 41' in vinfo['status'] and 'pinned' in vinfo['status'],
              json.dumps(vinfo)[:200])

        # action 5: Escape closes the comparison, focus stays in the page
        d.key('\ue00c'); actions += 1
        esc = d.wait("""(() => {const w = document.getElementById('cmpWrap');
                        return w.hidden ? [document.activeElement.tagName] : null;})()""", timeout=5)
        check('flow 5: Escape closes comparison; focus not lost (<body>)',
              bool(esc) and esc[0] != 'BODY', json.dumps(esc))
        vinfo2 = d.js("""return {info: document.getElementById('viewerInfo').textContent,
                                 status: document.getElementById('scStatus').textContent,
                                 primary: document.getElementById('bPrimary').textContent,
                                 dis: document.getElementById('bPrimary').disabled,
                                 another: document.getElementById('bAnother').textContent,
                                 ahidden: document.getElementById('bAnother').hidden};""")
        check('flow 5: inspector keeps the pinned wording; primary offers the previewed take, "Generate another take" sits beside it',
              'seed 41' in vinfo2['status'] and 'pinned' in vinfo2['status']
              and vinfo2['primary'] == 'Use this take' and not vinfo2['dis']
              and not vinfo2['ahidden'] and vinfo2['another'] == 'Generate another take',
              json.dumps(vinfo2)[:260])

        # action 6: start the next operation (real mouse click on "Generate another take")
        n0 = len(POSTS)
        d.click('#bAnother'); actions += 1
        prompt_rec = None
        end = time.time() + 30
        while time.time() < end and prompt_rec is None:
            for path, body in posts_after(n0):
                if path == '/prompt':
                    prompt_rec = body
            time.sleep(0.2)
        ups = [b for pth, b in posts_after(n0) if pth == '/upload/image']
        check('flow 6: next operation uploaded exactly the scene reference pictures first',
              len(ups) == 2 and sorted(re.findall(r'filename="([^"]+)"', ''.join(ups))) == ['picture-1.png', 'picture-2.png'],
              json.dumps([re.findall(r'filename="([^"]+)"', u) for u in ups]))
        sc4d, _ = scene_on_disk(slug, 3)
        graph = json.loads(prompt_rec)
        gen = [n for n in graph.get('prompt', {}).values() if n.get('class_type') == 'FreeVideoGenerate']
        gi = gen[0]['inputs'] if len(gen) == 1 else {}
        check('flow 6: /prompt body = graph + client_id + our prompt_id; graph node counts match one generate node',
              set(graph) == {'prompt', 'client_id', 'prompt_id'} and len(gen) == 1, json.dumps(sorted(graph)))
        check('flow 6: generate inputs are exactly the disk scene (prompt text, rerolled seed, size, steps)',
              gi.get('text') == sc4d['prompt'] and gi.get('seed') == sc4d['seed'] and gi.get('seed') != 42
              and gi.get('width') == 1344 and gi.get('height') == 768 and gi.get('seconds') == 5.0
              and gi.get('two_pass') is True and gi.get('base_steps') == 8 and gi.get('refine_steps') == 2
              and gi.get('force_regenerate') is False, json.dumps(gi)[:300])
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        job = api['scenes'][3].get('job') or {}
        check('flow 6: real job on disk-side API: render-draft, live, not unknown',
              job.get('kind') == 'render-draft' and not job.get('done')
              and job.get('state') in ('preparing', 'submitted', 'running', 'queued'), json.dumps(job)[:200])
        ex = d.wait("""(() => {const e = document.getElementById('scExec').textContent;
                       return e.includes('Running') && e.includes('render') ? e : null;})()""", timeout=20)
        navbadges = d.js("""const n = document.querySelectorAll('#navigator .navitem')[3];
                            return {state: n.querySelector('.navstate').textContent,
                                    q: document.getElementById('queueCount').textContent};""")
        check('flow 6: inspector reads "Running · render", navigator badge says Running',
              bool(ex) and 'Running' in navbadges['state'], json.dumps([ex, navbadges]))

        # action 7: open the queue drawer (real mouse click)
        d.click('#openQueue'); actions += 1
        qrows = d.wait("""(() => {const rows = [...document.querySelectorAll('#queueMine .qrow')];
                        return rows.length >= 1 ? rows.map(r => ({kind: r.querySelector('.qkind').textContent,
                                state: r.querySelector('.qstate').textContent,
                                times: r.querySelector('.qtime') ? r.querySelector('.qtime').textContent : ''}))
                              : null;})()""", timeout=10)
        mine = get_json(f'http://127.0.0.1:{WEB_PORT}/api/queue')['jobs']
        render_row = next((r for r in qrows if r['kind'] == 'render-draft'), None)
        check('flow 7: drawer lists this film live jobs (unknown scene-5 job absent until created; render-draft + counts right)',
              render_row is not None and len(qrows) == 1 + (1 if any(j['project'] == slug and j['state'] == 'unknown' for j in mine) else 0),
              json.dumps(qrows)[:300])
        check('flow 7: render row reads running + render with BOTH honest times (queued + running)',
              render_row and 'running' in render_row['state'].lower() and 'render' in render_row['state'].lower()
              and 'queued for' in render_row['times'] and 'running for' in render_row['times'],
              json.dumps(render_row))
        others = d.js("""return {wrap: !document.getElementById('queueOthersWrap').hidden,
                                  sum: document.getElementById('queueOthersSum').textContent};""")
        check('flow 7: other films collapse behind a summary naming the count',
              others['wrap'] and 'Other film' not in others['sum'] and 'other films (1)' in others['sum'],
              json.dumps(others))
        d.screenshot(SHOTS / '3-queue-drawer-running.png')

        # action 8: close the drawer (real mouse click); focus returns
        d.click('#closeQueue'); actions += 1
        closed = d.wait("(() => {const q = document.getElementById('queueDrawer'); "
                        "return !q.open ? document.activeElement.id : null;})()", timeout=5)
        check('flow 8: drawer closed; focus back on the Queue button',
              closed == 'openQueue', str(closed))
        check(f'flow completes in <= 8 user actions (counted {actions})', actions <= 8, str(actions))
        check('flow end: still no scroll at 1440x900', z() == [0, 0], str(z()))

        # ================ cancel the running job (real proof against the stub) ================
        d.js("""document.getElementById('openQueue').click(); return 1;""")
        d.wait("""(() => {const b = [...document.querySelectorAll('#queueMine .qrow button')]
                          .find(x => x.textContent === 'Cancel…'); return b ? 1 : null;})()""", timeout=8)
        d.js("""const b = [...document.querySelectorAll('#queueMine .qrow button')]
                      .find(x => x.textContent === 'Cancel…'); b.click();
                document.getElementById('cancelGo').click(); return 1;""")
        gone = d.wait("document.getElementById('queueCount').textContent === '0'", timeout=15)
        mine = get_json(f'http://127.0.0.1:{WEB_PORT}/api/queue')['jobs']
        check('cancel: Cancel… + confirm proves the stop (stub queue emptied), job cancelled',
              gone and all(j['state'] in ('cancelled', 'unknown') for j in mine if j['project'] == slug)
              and any(j['state'] == 'cancelled' for j in mine), json.dumps([j['state'] for j in mine])[:200])
        d.js("document.getElementById('closeQueue').click(); return 1;")

        # ================ enhance: /free + the real enhance graph, mp4 fallback ================
        n0 = len(POSTS)
        d.js(f"document.querySelectorAll('#navigator .navitem')[5].click(); return 1;")
        time.sleep(0.4)
        d.js("document.getElementById('bEnhanceSel').click(); return 1;")
        end = time.time() + 30
        epr = None
        while time.time() < end and epr is None:
            for path, body in posts_after(n0):
                if path == '/prompt':
                    epr = body
            time.sleep(0.2)
        free_seen = any(pth == '/free' for pth, _b in posts_after(n0))
        classes = {n.get('class_type') for n in json.loads(epr or '{}').get('prompt', {}).values()} if epr else set()
        sc6, _ = scene_on_disk(slug, 5)
        run6 = next(t['run'] for t in sc6['takes'])
        vids = {n['inputs'].get('video') for n in json.loads(epr or '{}').get('prompt', {}).values()
                if n.get('class_type') == 'VHS_LoadVideoPath'}
        prefixes = {n['inputs'].get('filename_prefix') for n in json.loads(epr or '{}').get('prompt', {}).values()
                    if n.get('class_type') == 'VHS_VideoCombine'}
        check('enhance: asks ComfyUI to free the GPU first, then submits one graph',
              free_seen and epr is not None, json.dumps([free_seen, bool(epr)]))
        check('enhance: graph carries SeedVR2 + RIFE + VHS load/combine nodes',
              any('SeedVR2' in c for c in classes) and any('RIFE' in c for c in classes)
              and 'VHS_LoadVideoPath' in classes and 'VHS_VideoCombine' in classes, json.dumps(sorted(classes)))
        check('enhance: loads the take video.mp4 directly (no raw frames in the fixture -> documented fallback)',
              vids == {str(Path(run6) / 'video.mp4')} and Path(run6, 'video.mp4').is_file(), json.dumps(sorted(vids)))
        check('enhance: output prefix names this take run as a director final',
              prefixes == {f'director/{Path(run6).name}-final'}, json.dumps(sorted(prefixes)))
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        ejob = api['scenes'][5].get('job') or {}
        check('enhance: real job kind enhance, live',
              ejob.get('kind') == 'enhance' and not ejob.get('done'), json.dumps(ejob)[:160])
        # cancel it (stub has no real work; delete makes the stop provable)
        d.js("""document.getElementById('openQueue').click(); return 1;""")
        d.wait("""(() => {const b = [...document.querySelectorAll('#queueMine .qrow button')]
                          .find(x => x.textContent === 'Cancel…'); return b ? 1 : null;})()""", timeout=8)
        d.js("""const b = [...document.querySelectorAll('#queueMine .qrow button')]
                      .find(x => x.textContent === 'Cancel…'); b.click();
                document.getElementById('cancelGo').click(); return 1;""")
        gone = d.wait("document.getElementById('queueCount').textContent === '0'", timeout=15)
        d.js("document.getElementById('closeQueue').click(); return 1;")
        check('enhance job cancelled against the stub', bool(gone))

        # ================ live batch: real ffmpeg animatic over the fixture media ================
        d.js("document.getElementById('bPreviewFilm').click(); return 1;")
        bar = d.wait("""(() => {const b = document.getElementById('batch');
                        if (!b.classList.contains('show')) return null;
                        const t = document.getElementById('batchText').textContent;
                        const w = document.getElementById('batchBar').getBoundingClientRect().width;
                        const c = document.getElementById('batchCancel');
                        return {t: t.length > 0, w: w, c: !!c && c.offsetParent !== null};})()""", timeout=15)
        check('batch: header bar shows with text, progress and a Cancel button while the animatic runs',
              bool(bar) and bar['t'] and bar['w'] >= 0 and bar['c'], json.dumps(bar))
        anim = d.wait("""(() => {const b = [...document.querySelectorAll('#viewerMode button')]
                                  .find(x => x.dataset.view === 'animatic');
                               if (!b) return null; b.click();
                               const s = document.getElementById('viewer').getAttribute('src') || '';
                               return s.includes('animatic') ? s : null;})()""", timeout=120)
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        check('batch: real ffmpeg assemble finished; the Animatic viewer mode plays the assembled file',
              bool(anim) and bool((api.get('outputs') or {}).get('animatic')), json.dumps([anim, (api.get('outputs') or {}).get('animatic')])[:220])
        d.js("""const b = [...document.querySelectorAll('#viewerMode button')].find(x => x.dataset.view === 'scene');
                b.click(); return 1;""")
        # export the film too, so the phone walk's #filmFlag has a real (then stale) output
        d.js("document.getElementById('bExportFilm').click(); return 1;")
        film = None
        end = time.time() + 180
        while time.time() < end:
            pv = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
            # the mp4 exists while ffmpeg is still writing it: only batch-done means the cut is finished
            if pv.get('batch') and pv['batch'].get('done') and (pv.get('outputs') or {}).get('film'):
                film = pv['outputs']['film']; break
            time.sleep(2)
        check('batch: Export film finished (batch done) with a real film output', bool(film), json.dumps(film)[:200])

        # ================ unknown job: an unanswered submit (stub drops /prompt) ================
        n0 = len(POSTS)
        # wait for the page to catch up with the finished batch: the reroll button must be live
        ready = d.wait("(() => {const b = document.getElementById('bAnother'); return b && !b.disabled ? 1 : null;})()", timeout=30)
        STATE['drop_next_prompt'] = True
        d.js(f"document.querySelectorAll('#navigator .navitem')[2].click(); return 1;")
        time.sleep(0.5)
        d.js("document.getElementById('bAnother').click(); return 1;")   # reroll + render; the submit is dropped
        unk = d.wait("""document.getElementById('unkJob') && !document.getElementById('unkJob').hidden
                        && document.getElementById('unkJob').textContent.includes('ComfyUI may still be executing')""", timeout=30)
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        ujob = api['scenes'][2].get('job') or {}
        check('unknown job: unanswered submit lands as unknown with the honest inspector notice',
              bool(unk) and ujob.get('state') == 'unknown', json.dumps(ujob)[:200])
        d.js("document.getElementById('openQueue').click(); return 1;")
        grow = d.wait("""(() => {const rows = [...document.querySelectorAll('#queueMine .qrow')];
                        const r = rows.find(x => /render/i.test(x.textContent) && /unknown/i.test(x.textContent));
                        return r ? {rec: [...r.querySelectorAll('button')].map(b => b.textContent)} : null;})()""", timeout=8)
        check('unknown job: drawer row offers Reconcile and Dismiss…',
              grow and 'Reconcile' in grow['rec'] and 'Dismiss…' in grow['rec'], json.dumps(grow))
        d.js("document.getElementById('closeQueue').click(); return 1;")

        # ================ reload: lazy contract holds again, then play + poll + focus ================
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        media = d.js("""return performance.getEntriesByType('resource')
                        .filter(e => /\\.(mp4|webm)(\\?|$)/.test(e.name)).map(e => e.name);""")
        check('lazy after reload: still zero .mp4/.webm fetched before any user action',
              media == [], json.dumps(media))
        d.click('#takeStrip .take')                     # user path: a take card click previews it
        d.wait("""(() => {const v = document.getElementById('viewer');
                        return !v.hidden && v.getAttribute('src') ? 1 : null;})()""", timeout=5)
        d.js("""const v = document.getElementById('viewer');
                window.__playerr = null;
                v.play().catch(e => { window.__playerr = e.name; }); return 1;""")
        t0 = d.wait("document.getElementById('viewer').currentTime > 0.05 ? document.getElementById('viewer').currentTime : null", timeout=10)
        dbg = d.js("""const v = document.getElementById('viewer');
                      return {err: window.__playerr, ready: v.readyState, src: (v.currentSrc || '').slice(-40),
                              paused: v.paused};""")
        d.js("document.getElementById('scPrompt').focus(); return 1;")
        for ch in 'ab':
            d.key(ch)
        typed = d.js("return document.getElementById('scPrompt').value;")
        check('playback: take plays from a plain click path (currentTime advanced)', bool(t0), json.dumps([t0, dbg]))
        d.js("document.getElementById('viewer').currentTime = 0; return 1;")   # rewind: the 5 s clip must still be mid-play at the check
        d.click('#openQueue'); time.sleep(0.3)          # real clicks so focus follows like a user's
        time.sleep(4)                                   # >= 2 drawer poll cycles + page poll
        mid = d.js("""return {val: document.getElementById('scPrompt').value,
                        inDrawer: document.getElementById('queueDrawer').contains(document.activeElement),
                        t: document.getElementById('viewer').currentTime,
                        playing: !document.getElementById('viewer').paused};""")
        d.click('#closeQueue');
        time.sleep(0.5)
        after = d.js("""return {val: document.getElementById('scPrompt').value,
                         focus: document.activeElement.id, scrollY: document.documentElement.scrollTop};""")
        check('polls: typed draft survives >= 2 poll cycles with the drawer open',
              mid['val'] == typed + 'ab' or mid['val'].endswith('ab'), json.dumps([typed, mid['val']])[:200])
        check('focus: keyboard focus stayed inside the open drawer (tab trap)', mid['inDrawer'])
        check('playback: video kept playing through the drawer and the polls',
              mid['playing'] and mid['t'] > t0, json.dumps([t0, mid['t']]))
        check('focus: drawer close returns focus to the Queue button; draft still intact',
              after['focus'] == 'openQueue' and after['val'].endswith('ab'), json.dumps(after)[:200])

        # ================ keyboard-only flow (NO mouse calls below) ================
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        never_body = True

        def afocus():
            nonlocal never_body
            el = d.js("""return {tag: document.activeElement.tagName, id: document.activeElement.id || '',
                                  cls: String(document.activeElement.className || '').split(' ')[0]};""")
            if el['tag'] == 'BODY':
                never_body = False
            return el

        d.js("document.querySelector('#navigator .navitem[aria-selected=\"true\"]').focus(); return 1;")
        d.key('\ue014')                                    # ArrowDown
        time.sleep(0.3)
        sel2 = d.js("""return document.querySelectorAll('#navigator .navitem')[1].getAttribute('aria-selected');""")
        d.key('\ue013')                                    # ArrowUp
        time.sleep(0.3)
        sel1 = d.js("""const it = document.querySelectorAll('#navigator .navitem');
                       return {sel: it[0].getAttribute('aria-selected'),
                               focus: it[0].contains(document.activeElement) || document.activeElement === it[0],
                               tag: document.activeElement.tagName,
                               label: (document.activeElement.getAttribute &&
                                       document.activeElement.getAttribute('aria-label') || '')};""")
        check('keys: ArrowDown/ArrowUp move the scene selection and follow focus',
              sel2 == 'true' and sel1['sel'] == 'true' and sel1['focus'], json.dumps([sel2, sel1])[:200])
        d.js("document.querySelector('#takeStrip [data-k=\"bDetails\"]').focus(); return 1;")
        d.key('\ue007')                                    # Enter
        tech = d.wait("document.getElementById('techDetails').open && document.querySelectorAll('#techBox .trow').length > 0", timeout=5)
        check('keys: Enter on a take card Details opens provenance rows', bool(tech))
        d.js("document.getElementById('bCompare').focus(); return 1;")
        d.key('\ue007')
        cmpopen = d.wait("!document.getElementById('cmpWrap').hidden", timeout=5)
        d.key('\ue00c')                                    # Escape
        f = afocus()
        check('keys: comparison opens with Enter and Escape closes it, focus back on Compare',
              bool(cmpopen) and f['id'] == 'bCompare', json.dumps(f))
        d.js("document.getElementById('bAnother').focus(); return 1;")
        n0 = len(POSTS)
        d.key('\ue007')                                    # Enter -> reroll + render (real /prompt)
        end = time.time() + 25
        np = 0
        while time.time() < end:
            np = sum(1 for pth, _ in posts_after(n0) if pth == '/prompt')
            if np: break
            time.sleep(0.2)
        check('keys: Enter on the primary action started a real render (POST /prompt)', np == 1, str(np))
        d.js("document.getElementById('openQueue').focus(); return 1;")
        d.key('\ue007')
        drawer = d.wait("!document.getElementById('queueDrawer').hidden && document.getElementById('queueDrawer').open", timeout=5)
        f = afocus()
        check('keys: Queue opens with Enter; focus moves into the dialog', bool(drawer) and f['tag'] != 'BODY', json.dumps(f))
        d.js("""const b = [...document.querySelectorAll('#queueMine .qrow button')]
                       .find(x => x.textContent === 'Cancel…'); if (b) b.focus(); return !!b;""")
        d.key('\ue007')                                    # Cancel…
        cask = d.wait("!!(document.getElementById('cancelAsk').open)", timeout=5)
        d.js("document.getElementById('cancelGo').focus(); return 1;")
        d.key('\ue007')                                    # Enter on the confirm
        gone = d.wait("document.getElementById('queueCount').textContent === '1'", timeout=15)   # only unknown stays
        check('keys: Cancel… -> confirm dialog -> Enter cancels the job (only the unknown one stays live)',
              cask and gone, json.dumps([cask, gone]))
        d.key('\ue00c')                                    # Escape closes the drawer
        f = afocus()
        check('keys: Escape closes the drawer, focus returns to Queue',
              f['id'] == 'openQueue', json.dumps(f))
        d.js("document.getElementById('openSettings').focus(); return 1;")
        d.key('\ue007')
        sopen = d.wait("document.getElementById('settings').open", timeout=5)
        f = afocus()
        check('keys: Settings opens with Enter; focus lands inside', bool(sopen) and f['id'] != 'openSettings', json.dumps(f))
        d.key('\ue00c')
        f = afocus()
        check('keys: Escape closes Settings, focus returns', f['id'] == 'openSettings', json.dumps(f))
        # arrows only act inside the navigator (real page semantics): focus the selected row, then walk down
        d.js("document.querySelector('#navigator .navitem[aria-selected=\"true\"]').focus(); return 1;")
        for _ in range(6):
            cur = d.js("return [...document.querySelectorAll('#navigator .navitem')].findIndex(n => n.getAttribute('aria-selected') === 'true');")
            if cur == 2:
                break
            d.key('\ue014')                                # ArrowDown steps the selection toward scene 3
            time.sleep(0.25)
        d.js("document.getElementById('unkDismiss').focus(); return 1;")
        d.key('\ue007')
        dask = d.wait("!!(document.getElementById('dismissAsk').open)", timeout=5)
        f = afocus()
        check('keys: unknown-job Dismiss… dialog opens with Enter; focus inside the dialog',
              bool(dask) and f['tag'] != 'BODY', json.dumps(f))
        d.key('\ue00c')
        f = afocus()
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        ustate = (api['scenes'][2].get('job') or {}).get('state')
        check('keys: Escape closes Dismiss, focus returns to Dismiss…; the job stays unknown',
              f['id'] == 'unkDismiss' and ustate == 'unknown', json.dumps([f, ustate]))
        check('keys: focus never landed on <body> after any close', never_body)

        # ================ 1920x1080 spot ================
        d.resize(1920, 1080)
        d.js(f"document.querySelectorAll('#navigator .navitem')[3].click(); return 1;")
        wide = d.js("""const p = document.getElementById('bPrimary');
                       const na = document.getElementById('nextAction').getBoundingClientRect();
                       return {t: p.textContent, dis: p.disabled, scroll: [document.documentElement.scrollTop, document.body.scrollTop],
                               na: na.left >= 0 && na.right <= innerWidth};""")
        check('1920x1080: pinned scene primary reads "Generate another take" and is enabled; no scroll',
              wide['t'] == 'Generate another take' and not wide['dis'] and wide['scroll'] == [0, 0] and wide['na'],
              json.dumps(wide))

        # ================ phone 390x844: walk every capability-map row ================
        d.resize(500, 930)
        d.go('about:blank')
        d.js("""document.write('<body style="margin:0;background:#101014"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        fid = d.frame_on('#__f')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=25)
        inner = d.js('return [window.innerWidth, window.innerHeight, document.documentElement.scrollTop];')
        check('phone: frame viewport is 390x844 and the page loads unscrolled',
              inner == [390, 844, 0], str(inner))
        shot_parent = d.frame_off()                      # element screenshot needs the top context
        f_el = d.element('#__f')
        d.element_screenshot(f_el, SHOTS / '1-mobile-load.png')
        d.frame_on(fid)

        # parse the capability map
        rows = []
        for line in CAPMAP.read_text().splitlines():
            if not line.startswith('|'):
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) != 6 or 'Old control' in cells[0] or set(cells[0]) <= set('-: '):
                continue
            toks = [t.strip() for g in re.findall(r'`([^`]+)`', cells[4]) for t in g.split(',') if t.strip()]
            walk = [t for t in toks if t[:1] in '#.[']
            rows.append({'what': cells[0], 'status': cells[3], 'toks': toks, 'walk': walk,
                         'verified': cells[5]})
        check('capability map: every kept/moved/live row carries selectors and a verifier',
              all(r['toks'] and r['verified'] for r in rows),
              json.dumps([r['what'] for r in rows if not (r['toks'] and r['verified'])])[:300])

        TOK_PROC = {
            '#newBox': 'new', '#nSeconds': 'new', '#nAspect': 'new',
            '#createBtn': 'new', '.slot': 'new', '#nLibName': 'new', '#nLibAdd': 'new',
            '#libName': 'settings', '#libAdd': 'settings', '#libList': 'settings', '#libDesc': 'settings',
            '#pChars': 'settings', '#pCharsApply': 'settings', '#pBible': 'settings', '#pNotes': 'settings',
            '#loras': 'settings', '#addLora': 'settings', '#loraHint': 'settings', '#pMusicBrief': 'settings',
            '#musicBtn': 'settings', '#musicName': 'settings', '#pTitleCardText': 'settings',
            '#pTitleCardSecs': 'settings', '#pEndCardText': 'settings', '#pEndCardSecs': 'settings',
            '#pTitle': 'settings', '#pSizeText': 'settings', '#freeSpace': 'settings',
            '.stage': 'scene', '.stage .caret': 'menu', '.caret': 'menu', '#stageMenu': 'menu',
            '#techDetails': 'tech', '#techBox': 'tech', '.trow': 'tech',
            '[data-k=bDetails]': 'tech', '[data-k=bUse]': 'tech',
            '#bAnother': 'another',
            '#cmpWrap': 'cmp', '#cmpBar': 'cmp', '#cmpPlay': 'cmp', '#cmpSeek': 'cmp',
            '#cmpAudio': 'cmp', '#cmpDiff': 'cmp', '#bCompare': 'cmp',
            '#contBox': 'cont', '#contRegen': 'cont',
            '#unkJob': 'unk', '#unkReconcile': 'unk', '#unkDismiss': 'unk',
            '#primaryWhy': 'unk', '#scExec': 'unk',
            '#dismissAsk': 'dismiss', '#dismissGo': 'dismiss',
            '#compareWhy': 'whys', '#anotherWhy': 'whys', '#enhanceWhy': 'whys',
            '#batch': 'batch', '#batchText': 'batch', '#batchBar': 'batch', '#batchCancel': 'batch',
            '#bPreviewFilm': 'batch', '#bExportFilm': 'batch',
            '#queueDrawer': 'queue', '#queueMine': 'queue', '.qrow': 'queue', '.qkind': 'queue',
            '.qstate': 'queue', '.qtime': 'queue', '.qnote': 'queue', '#closeQueue': 'queue',
            '#openQueue': 'queue', '#queueOthersWrap': 'others',
            '#cancelAsk': 'cancelask', '#cancelAskWhat': 'cancelask', '#cancelGo': 'cancelask',
            '#hist': 'hist', '#histList': 'hist',
            '#animFlag': 'stale', '#filmFlag': 'stale',
            '#picker': 'scene', '#newBtn': 'scene', '#saveStatus': 'scene', '#nextAction': 'scene',
            '#navSay': 'scene', '.navstate': 'scene', '#viewer': 'scene', '#viewerMode': 'scene',
            '#timeline': 'scene', '.tl': 'scene', '#takeStrip': 'scene', '.take': 'scene',
            '#scTitle': 'scene', '#scSeconds': 'scset', '#scConnect': 'scset', '#scTransition': 'scset',
            '#scAction': 'scene', '#scPrompt': 'scene', '#scValidity': 'scene', '#bPrimary': 'scene',
            '#bEnhanceSel': 'scene', '#bFinal': 'scene', '#bVariants': 'scene', '#vCount': 'scene',
            '#bAddScene': 'scset', '#bDeleteScene': 'scset', '#bMoveUp': 'scset', '#bMoveDown': 'scset',
        }
        missing_proc = sorted({t for r in rows for t in r['walk'] if t not in TOK_PROC and t[:1] in '#.['})
        check('phone walk: every walkable selector token has a panel to open it', not missing_proc,
              json.dumps(missing_proc))

        def open_panel(p):
            if p == 'new':
                d.js("document.getElementById('newBtn').click(); return 1;")
                d.wait("!document.getElementById('newBox').hidden", timeout=5)
            elif p == 'settings':
                d.js("document.getElementById('openSettings').click(); return 1;")
                d.wait("document.getElementById('settings').open", timeout=5)
            elif p == 'menu':
                d.js("document.querySelector('.stage .caret').click(); return 1;")
                d.wait("(() => {const m = document.getElementById('stageMenu'); return m && m.offsetParent !== null;})()", timeout=5)
            elif p == 'tech':
                d.js(f"document.querySelectorAll('#navigator .navitem')[3].click(); return 1;")
                time.sleep(0.3)
                d.js("""const b = document.querySelector('#takeStrip [data-k="bDetails"]'); b.click(); return 1;""")
                d.wait("document.getElementById('techDetails').open && document.querySelector('#techBox .trow')", timeout=5)
            elif p == 'another':
                d.js("""const b = document.querySelector('#takeStrip [data-k="bDetails"]'); b.click();
                        return document.getElementById('bAnother') && !document.getElementById('bAnother').hidden;""")
                d.wait("!document.getElementById('bAnother').hidden", timeout=5)
            elif p == 'cmp':
                d.js("""document.getElementById('bCompare').click(); return 1;""")
                d.wait("!document.getElementById('cmpWrap').hidden", timeout=5)
            elif p == 'cont':
                # the continuation scene only speaks when its upstream take moved: move it
                d.js("document.querySelectorAll('#navigator .navitem')[0].click(); return 1;")
                time.sleep(0.6)
                d.js("""const card = [...document.querySelectorAll('#takeStrip .take')].find(c => c.querySelector('[data-k=bUse]'));
                        if (card) card.querySelector('[data-k=bUse]').click(); return !!card;""")
                d.wait("""(() => {const it = document.querySelector('#navigator .navitem[aria-selected="true"]');
                                return it && it.textContent.includes('Running on') ? 1 : null;})()""", timeout=5)
                d.js("document.querySelectorAll('#navigator .navitem')[1].click(); return 1;")
                d.wait("!document.getElementById('contBox').hidden", timeout=6)
            elif p == 'unk':
                d.js(f"document.querySelectorAll('#navigator .navitem')[2].click(); return 1;")
                d.wait("!document.getElementById('unkJob').hidden", timeout=6)
            elif p == 'dismiss':
                d.js("document.getElementById('unkDismiss').click(); return 1;")
                d.wait("!!(document.getElementById('dismissAsk').open)", timeout=5)
            elif p == 'whys':
                # each reason span surfaces in exactly one reachable state; visit those states
                d.js("""document.querySelectorAll('#navigator .navitem')[1].click(); return 1;""")
                d.wait("!document.getElementById('compareWhy').hidden", timeout=6)
                d.js("""window.confirm = () => true;                    // the page's own delete/add confirms
                        document.getElementById('bAddScene').click(); return 1;""")
                d.wait("""(() => {if (document.querySelectorAll('#navigator .navitem').length !== 7) return null;
                                document.querySelectorAll('#navigator .navitem')[6].click();
                                return !document.getElementById('anotherWhy').hidden
                                       && !document.getElementById('enhanceWhy').hidden ? 1 : null;})()""", timeout=12)
            elif p == 'batch':
                GATE.unlink(missing_ok=True)       # stall the encode: the walk measures a live batch
                d.js("""const b = document.getElementById('bPreviewFilm'); if (!b.disabled) b.click(); return 1;""")
                d.wait("document.getElementById('batch').classList.contains('show')", timeout=20)
            elif p == 'queue':
                d.js("document.getElementById('openQueue').click(); return 1;")
                d.wait("document.getElementById('queueDrawer').open && document.querySelectorAll('#queueMine .qrow').length >= 1", timeout=8)
            elif p == 'others':
                d.js("""const w = document.getElementById('queueOthersWrap');
                        if (w.hidden) document.getElementById('queueOthersSum').click();
                        return !w.hidden;""")
                d.wait("!document.getElementById('queueOthersWrap').hidden", timeout=5)
            elif p == 'cancelask':
                # the dialog only exists beside a live job: start one, then reach its Cancel… in the drawer
                d.js("document.querySelectorAll('#navigator .navitem')[1].click(); return 1;")
                time.sleep(0.6)
                d.js("""const b = document.getElementById('bAnother');
                        if (!b.disabled && !b.hidden) b.click(); return 1;""")
                d.js("if (!document.getElementById('queueDrawer').open) document.getElementById('openQueue').click(); return 1;")
                d.wait("""(() => {const b = [...document.querySelectorAll('#queueDrawer .qrow button')]
                                  .find(x => x.textContent === 'Cancel\u2026'); return b ? 1 : null;})()""", timeout=15)
                d.js("""const b = [...document.querySelectorAll('#queueDrawer .qrow button')]
                              .find(x => x.textContent === 'Cancel…'); b.click(); return 1;""")
                d.wait("!!(document.getElementById('cancelAsk').open)", timeout=5)
            elif p == 'hist':
                d.js("""document.querySelectorAll('#navigator .navitem')[0].click(); return 1;""")
                time.sleep(0.3)
                d.js("""const t = document.getElementById('scPrompt');
                        t.value = t.value + ' A quiet wind crosses the grass.';
                        t.dispatchEvent(new Event('input', {bubbles: true})); return 1;""")
                d.wait("!document.getElementById('hist').hidden && document.querySelector('#histList .histrow, #histList button')", timeout=8)
                d.js("""const s = document.querySelector('#hist summary'); if (!document.getElementById('hist').open) s.click();
                        return document.getElementById('hist').open;""")
            elif p == 'stale':
                # a stale flag needs real drift: move a transition (a cut component) after the cuts were made
                d.js("""document.querySelectorAll('#navigator .navitem')[0].click(); return 1;""")
                time.sleep(0.5)
                d.js("""const t = document.getElementById('scTransition');
                        if (t.value !== 'crossfade') { t.value = 'crossfade'; t.dispatchEvent(new Event('change', {bubbles: true})); }
                        return 1;""")
                d.wait("document.getElementById('animFlag') && !document.getElementById('animFlag').hidden"
                       " && document.getElementById('filmFlag') && !document.getElementById('filmFlag').hidden", timeout=15)
            elif p == 'scene':
                d.js(f"document.querySelectorAll('#navigator .navitem')[3].click(); return 1;")
                time.sleep(0.3)
            elif p == 'scset':
                # the scene row lives behind its own disclosure; open it like a user would
                d.js("""const d = document.getElementById('scSettings');
                        if (!d.open) d.querySelector('summary').click(); return d.open;""")
                d.wait("document.getElementById('scSettings').open", timeout=5)

        PANELS = ['scene', 'scset', 'settings', 'menu', 'tech', 'another', 'cmp', 'cont', 'unk', 'dismiss',
                  'whys', 'hist', 'stale', 'batch', 'queue', 'others', 'cancelask', 'new']
        seen = set()
        fails = []
        for panel in PANELS:
            tokens = [t for r in rows for t in r['walk'] if TOK_PROC.get(t, 'scene') == panel and t not in seen]
            if not tokens:
                continue
            open_panel(panel)
            if panel == 'queue':
                d.frame_off()                        # element screenshot needs the top context
                d.element_screenshot(d.element('#__f'), SHOTS / '2-mobile-queue.png')
                d.frame_on(fid)
            for tok in tokens:
                seen.add(tok)
                m = d.js("""const el = document.querySelector(arguments[0]);
                            if (!el) return {exists: false};
                            el.scrollIntoView({block: 'center', inline: 'center'});
                            const cs = getComputedStyle(el), r = el.getBoundingClientRect();
                            return {exists: true, vis: cs.display !== 'none' && cs.visibility !== 'hidden'
                                          && (r.width > 0 || r.height > 0),
                                    h: r.height, w: r.width, l: r.left, rt: r.right, inter: el.matches(
                                      'button, input, select, textarea, a[href]'),
                                    hid: el.hasAttribute('hidden'), disp: cs.display,
                                    txt: (el.textContent || '').trim().slice(0, 40)};""", tok)
                bad = None
                if not m.get('exists'):
                    bad = 'missing'
                elif not m.get('vis'):
                    bad = f"not visible (hidden={m['hid']}, display={m['disp']}, text={m['txt']!r})"
                elif m['l'] < -0.5 or m['rt'] > 390.5:
                    bad = f"outside viewport width ({m['l']:.0f}..{m['rt']:.0f})"
                elif tok in ('.stage .caret', '.caret'):
                    if m['w'] < 44: bad = f"caret only {m['w']:.0f}px wide"
                elif m.get('inter') and m['h'] < 44:
                    bad = f"control only {m['h']:.0f}px tall"
                if bad:
                    fails.append(f'{tok} [{panel}]: {bad}')
            # leave the panel the same way a user would
            if panel in ('settings', 'cmp'):
                d.key('\ue00c')
            elif panel == 'menu':
                d.js("document.body.click(); return 1;")
            elif panel == 'dismiss':
                # Dismiss… -> Dismiss: resolve the unknown job for real
                d.js("document.getElementById('dismissGo').click(); return 1;")
                d.wait("!document.getElementById('dismissAsk').open", timeout=5)
            elif panel == 'batch':
                # Cancel… acts for real: the flag is set and assemble's monitor kills the live
                # encode; re-opening the gate afterwards keeps every later ffmpeg fast.
                d.js("const b = document.getElementById('batchCancel'); if (b) b.click(); return 1;")
                time.sleep(0.5)
                GATE.write_text('open')
                time.sleep(1.0)
            elif panel == 'whys':
                # remove the throwaway scene the reason walk added (confirm already overridden)
                d.js("document.getElementById('bDeleteScene').click(); return 1;")
                d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=8)
            elif panel == 'cancelask':
                d.js("""if (document.getElementById('cancelAsk').open) document.getElementById('cancelAsk').close();
                        if (document.getElementById('queueDrawer').open) document.getElementById('queueDrawer').close(); return 1;""")
        check('phone walk: every capability-map selector visible, in-viewport, controls >= 44px',
              not fails, json.dumps(fails)[:1500])
        check('phone walk: walked every walkable token', len(seen) >= 40, str(len(seen)))
        # dismissing the unknown job from the phone walk changed real state
        api = get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')
        u4 = (api['scenes'][2].get('job') or {})
        check('phone: Dismiss… + Dismiss acted for real (job resolved as dismissed, holds freed)',
              (u4.get('resolved') or {}).get('verdict') == 'dismissed' and (u4.get('resolved') or {}).get('by') == 'user',
              json.dumps(u4)[:200])
        # batch cancel button acted for real: the worker aborts at its next checkpoint
        deadline = time.time() + 120
        bstate = api.get('batch') or {}
        while not bstate.get('done') and time.time() < deadline:
            time.sleep(1.0)
            bstate = (get_json(f'http://127.0.0.1:{WEB_PORT}/api/p/{slug}')).get('batch') or {}
        check('phone: batch Cancel… stopped the live animatic batch',
              bool(bstate.get('done')) and 'cancel' in str(bstate.get('error') or '').lower(),
              json.dumps(bstate)[:200])
        d.frame_off()

        # ================ session B: dark theme ================
        d.close()
        time.sleep(1.0)                               # let the geckodriver port release
        d = Driver(prefs={'layout.css.prefers-color-scheme.content-override': 0})
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.resize(1440, 900)
        bg = d.js("""const c = s => getComputedStyle(document.querySelector(s)).backgroundColor;
                     return [c('body'), c('html')];""")
        check('dark: page paints the dark palette (--bg #141312)',
              'rgb(20, 19, 18)' in bg, json.dumps(bg))
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.getElementById('queueDrawer').open", timeout=5)
        vis = d.js("""const ok = s => {const e = document.querySelector(s);
                        if (!e) return s + '?'; const r = e.getBoundingClientRect();
                        return e.offsetParent !== null && r.left >= -1 && r.right <= innerWidth + 1 && r.top <= innerHeight ? 1 : s;};
                        return ['#openQueue','#queueMine','#closeQueue','.qrow','#bPrimary','#viewer']
                               .map(ok).filter(x => x !== 1);""")
        check('dark: core controls visible and in-viewport with the drawer open', vis == [], json.dumps(vis))
        d.screenshot(SHOTS / '4-dark-drawer.png')
        d.js("document.getElementById('closeQueue').click(); return 1;")
        d.resize(500, 930)
        d.go('about:blank')
        d.js("""document.write('<body style="margin:0"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        fid2 = d.frame_on('#__f')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=25)
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.getElementById('queueDrawer').open", timeout=5)
        bg2 = d.js("""const c = s => getComputedStyle(document.querySelector(s)).backgroundColor;
                      return [window.innerWidth, window.innerHeight, c('body'), c('html')];""")
        check('dark phone: 390x844 renders dark with the drawer open',
              bg2[0] == 390 and bg2[1] == 844 and 'rgb(20, 19, 18)' in bg2[2:], json.dumps(bg2))
        d.frame_off()
        d.element_screenshot(d.element('#__f'), SHOTS / '3-mobile-dark.png')

        sizes = sorted(p.stat().st_size for p in SHOTS.glob('*.png'))
        check(f'screenshots: {len(sizes)} files, each under 400 KB',
              len(sizes) == 7 and all(s < 400 * 1024 for s in sizes), json.dumps([s // 1024 for s in sizes]))
    finally:
        if d:
            d.close()
        webp.terminate()
        stub.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(results)
    print(f'\n{passed}/{len(results)} acceptance checks passed; screenshots -> {SHOTS}')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
