"""Browser acceptance of the three readiness chips: one headless Firefox session against the
six-scene fixture, with the backend split so each check tells a different truth:

  * a stub ComfyUI (tests/stub_comfy.py) that serves the render node set, the LoRA picker and
    the finishing model files, but with the three SeedVR2 classes DROPPED (COMFY_STUB_DROP):
    Video must read Ready, Finishing must read Down naming the SeedVR2 upscaler;
  * DIRECTOR_URL pinned to a closed port: Prompts reads Down.

The page then has to gate its own actions off that state, without the server ever refusing a
click: the primary action on a prompted scene becomes 'Create prompt', disabled with the
prompt-model reason; Enhance/Final/Export carry the finishing reason ending in
'(affects Enhance, Final, Export)'; 'Generate another take' (video, Ready) stays enabled.
Keyboard: focusing the Finishing chip and pressing Enter opens the details panel, which lists
the SeedVR2 row with its fix line, and Escape closes it. The chips stay inside the header's
layout budget at 390x844 as well.

Needs `geckodriver` and `firefox` on PATH. Throwaway projects dir and throwaway ComfyUI root;
never touches real projects. Run: python3 tests/browser_readiness.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import project as projects   # noqa: E402
import make_fixture          # noqa: E402

WEB_PORT, STUB_PORT, DRIVER_PORT = 8211, 8212, 4465
SEEDVR2 = 'SeedVR2LoadDiTModel,SeedVR2LoadVAEModel,SeedVR2VideoUpscaler'
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(('ok   - ' if ok else 'FAIL - ') + name + (f'  [{detail}]' if detail and not ok else ''))


class Driver:
    def __init__(self):
        self.base = f'http://127.0.0.1:{DRIVER_PORT}'
        self.proc = subprocess.Popen(['geckodriver', '--port', str(DRIVER_PORT)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.call('GET', '/status'); break
            except Exception:
                time.sleep(0.2)
        caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox',
                                                'moz:firefoxOptions': {'args': ['-headless']}}}}
        ses = self.call('POST', '/session', caps)
        if 'sessionId' not in ses.get('value', {}):
            raise RuntimeError('geckodriver session failed: ' + json.dumps(ses)[:300])
        self.sid = ses['value']['sessionId']

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
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

    def wait(self, script, timeout=15, *args):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js('return (' + script + ')', *args)
            if v: return v
            time.sleep(0.15)
        return None

    def resize(self, w, h):
        for _ in range(6):
            cur_w, cur_h = self.js('return [window.innerWidth, window.innerHeight]')
            if (cur_w, cur_h) == (w, h):
                return
            rect = self.s('GET', '/window/rect')
            self.s('POST', '/window/rect', {'width': rect['width'] + (w - cur_w),
                                            'height': rect['height'] + (h - cur_h)})
            time.sleep(0.3)
        raise RuntimeError(f'cannot resize viewport to {w}x{h}')

    def key(self, ch):
        self.s('POST', '/actions', {'actions': [{'type': 'key', 'id': 'k',
                                                 'actions': [{'type': 'keyDown', 'value': ch},
                                                             {'type': 'keyUp', 'value': ch}]}]})

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


CHIP = """const c = document.getElementById('rd-' + arguments[0]);
  return {text: c.textContent.replace(/\\s+/g, ' ').trim(),
          down: c.classList.contains('down'), limited: c.classList.contains('limited'),
          label: c.getAttribute('aria-label') || ''};"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-readiness-'))
    man = make_fixture.build(tmp)            # real ffmpeg media; sets projects.ROOT
    slug, ids = man['slug'], man['scene_ids']
    comfy_root = tmp / 'comfy-stub'
    comfy_root.mkdir()
    stub = subprocess.Popen([sys.executable, str(HERE / 'tests' / 'stub_comfy.py'),
                             '--port', str(STUB_PORT), '--root', str(comfy_root)],
                            env=dict(os.environ, COMFY_STUB_DROP=SEEDVR2),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; '
                'project.ROOT = Path(%r); sys.argv = ["web.py", "--port", "%d"]; '
                'import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    web = subprocess.Popen([sys.executable, '-c', launcher],
                           env=dict(os.environ,
                                    COMFY_URL=f'http://127.0.0.1:{STUB_PORT}',
                                    DIRECTOR_URL='http://127.0.0.1:1/v1/chat/completions',
                                    COMFY_ROOT=str(comfy_root),
                                    JOBS_DB=str(tmp / 'jobs.db')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1); break
            except Exception: time.sleep(0.2)
        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6")

        # the split verdict: prompts down (closed director), video ready (full stub),
        # finishing down (stub missing SeedVR2)
        ok = d.wait("""['prompt', 'finishing'].every(k =>
                         document.getElementById('rd-' + k).classList.contains('down'))
                       && !document.getElementById('rd-video').classList.contains('down')
                       && !document.getElementById('rd-video').classList.contains('limited')""")
        check('three chips split: Prompts Down, Video Ready, Finishing Down', bool(ok),
              json.dumps([d.js(CHIP, k) for k in ('prompt', 'video', 'finishing')])[:400])
        chips = {k: d.js(CHIP, k) for k in ('prompt', 'video', 'finishing')}
        check('chip words not colours: Down/Ready/Down',
              chips['prompt']['text'].endswith('Down') and chips['video']['text'].endswith('Ready')
              and chips['finishing']['text'].endswith('Down'),
              json.dumps(chips)[:400])
        check('finishing chip names the missing SeedVR2 upscaler',
              'seedvr2' in chips['finishing']['label'].lower(), chips['finishing']['label'])
        check('prompts chip says which address did not answer',
              '127.0.0.1:1' in chips['prompt']['label'], chips['prompt']['label'])
        check('video chip states the ready reason',
              'ready' in chips['video']['label'].lower(), chips['video']['label'])

        # server-side truth, same shape the chips read
        rep = json.load(urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/readiness', timeout=5))
        levels = {k: c['level'] for k, c in rep['checks'].items()}
        fin_hard = [i['name'] for i in rep['checks']['finishing']['items'] if i['hard'] and not i['ok']]
        check('GET /api/readiness: prompt+finishing unavailable, video ready',
              levels == {'prompt': 'unavailable', 'video': 'ready', 'finishing': 'unavailable'},
              json.dumps(levels))
        check('the one hard finishing item is the SeedVR2 upscaler',
              any('SeedVR2' in n for n in fin_hard), json.dumps(fin_hard))
        check('the prompt reason names the closed port',
              '127.0.0.1:1' in rep['checks']['prompt']['reason'], rep['checks']['prompt']['reason'])

        # select scene 1 (prompted, has takes): the gating shows where the actions are
        d.js("""document.querySelectorAll('#navigator .navitem')[0].click(); return 1;""")
        d.wait("document.getElementById('inspector').textContent.includes('Scene 1')")
        gate = d.js("""
          const g = id => { const b = document.getElementById(id);
                            return {dis: b.disabled, title: b.title || ''}; };
          const prim = document.getElementById('bPrimary');
          return {prim: {t: prim.textContent, dis: prim.disabled},
                  enh: g('bEnhanceSel'), fin: g('bFinal'), exp: g('bExportFilm')};""")
        check('finishing-down reasons are one sentence ending in the affected actions',
              gate['enh']['dis'] and gate['enh']['title'].endswith('(affects Enhance, Final, Export)')
              and gate['fin']['dis'] and gate['fin']['title'].endswith('(affects Enhance, Final, Export)')
              and gate['exp']['dis'],
              json.dumps(gate)[:500])
        check('enhance reason names the missing SeedVR2 item',
              'SeedVR2' in gate['enh']['title'], gate['enh']['title'])
        check('video is Ready so the take action stays enabled',
              gate['prim']['t'] == 'Generate another take' and not gate['prim']['dis'],
              json.dumps(gate['prim'])[:300])
        # clear this scene's prompt: the primary action becomes 'Create prompt', disabled with
        # the prompt-model reason - the gate follows the check, not the other buttons
        d.js("""const t = document.getElementById('scPrompt');
                t.value = ''; t.dispatchEvent(new Event('input', {bubbles: true}));
                t.blur(); return 1;""")
        ok = d.wait("document.getElementById('bPrimary').textContent === 'Create prompt'")
        prim = d.js("""const b = document.getElementById('bPrimary');
                       return {dis: b.disabled, why: document.getElementById('primaryWhy').textContent};""")
        check('a promptless scene gets Create prompt disabled with the model reason',
              bool(ok) and prim['dis'] and prim['why'].startswith('Prompt model not reachable')
              and '127.0.0.1:1' in prim['why'], json.dumps(prim)[:300])

        # keyboard only: Enter on the focused Finishing chip opens the details panel
        d.js("document.getElementById('rd-finishing').focus(); return 1;")
        d.key('\ue007')
        ok = d.wait("!document.getElementById('rd-panel').hidden "
                    "&& document.getElementById('rd-panel').textContent.includes('SeedVR2')")
        check('Enter on the Finishing chip opens the panel with the SeedVR2 row and its fix',
              bool(ok) and 'fix:' in d.js("return document.getElementById('rd-panel').textContent"),
              d.js("return document.getElementById('rd-panel').textContent")[:300])
        check('the chip reports itself expanded',
              d.js("return document.getElementById('rd-finishing').getAttribute('aria-expanded');") == 'true')
        d.key('\ue00c')
        check('Escape closes the panel',
              d.js("return document.getElementById('rd-panel').hidden;"))

        # a fresh /object_info still keeps Video Ready - and the gate stays open
        code = urllib.request.urlopen(
            f'http://127.0.0.1:{WEB_PORT}/api/readiness/video?refresh=1', timeout=5).getcode()
        check('per-check refresh endpoint answers for one check', code == 200, str(code))

        # phone: headless firefox floors real windows at 500 px wide, so measure the chips in
        # an exact 390x844 iframe (the same trick browser_layout uses)
        d.resize(500, 930)
        d.js("""document.write('<body style="margin:0;background:#101014"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        d.s('POST', '/frame', {'id': d.s('POST', '/element', {'using': 'css selector', 'value': '#__f'})})
        ok = d.wait("document.querySelectorAll('#readiness .rchip').length === 3", timeout=20)
        m = d.js("""window.scrollTo(0, 0);
          const chips = [...document.querySelectorAll('#readiness .rchip')];
          return {vw: window.innerWidth,
                  hdr: document.getElementById('hdr').offsetHeight + document.getElementById('stages').offsetHeight,
                  visible: chips.filter(c => c.offsetParent !== null).length,
                  heights: chips.map(c => c.getBoundingClientRect().height),
                  hscroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth};""")
        d.s('POST', '/frame', {'id': None})
        check('phone 390x844: three chips visible, header budget holds, no horizontal scroll',
              bool(ok) and m['vw'] == 390 and m['visible'] == 3 and m['hdr'] <= 190
              and m['hscroll'] and min(m['heights']) >= 28, json.dumps(m))
    finally:
        if d: d.close()
        for p in (web, stub):
            p.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(results)
    print(f'{passed}/{len(results)} checks passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
