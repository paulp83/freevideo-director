"""Browser check of Stage B1 in the workspace project.html: take comparison, validity vs
execution wording, disabled-action reasons, unknown-job Reconcile/Dismiss, continuity context.

Runs headless Firefox through geckodriver's raw WebDriver HTTP API against the six-scene fixture
(real ffmpeg media; `media.autoplay.blocking_policy=0` so the comparison player can start in the
headless sandbox). web.py starts in-process on a spare port with three real jobs registered
through the real job API before serving: an enhance job mid-flight on scene 2, an unknown
render on the legacy scene 5 (prompt ids empty on purpose, so Reconcile answers "cannot tell"
without ever contacting ComfyUI), and a failed render on scene 6 ("ComfyUI unreachable" - which
COMFY_URL points at a closed port, so the page's own probe also deterministically reports the
renderer unavailable). Nothing is simulated in the page: every string checked here is produced
by project.html reading real /api state, and the take changes are written to the real project
on disk, read back from the parent process.

Screenshots of the four new surfaces land in docs/screenshots/m3-stage-b/ for human review.
Needs `geckodriver` and `firefox` on PATH. Throwaway projects dir; never touches real projects.
Run: python3 tests/browser_compare.py
"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import project as projects   # noqa: E402
import make_fixture          # noqa: E402

WEB_PORT, DRIVER_PORT = 8207, 4461
SHOTS = HERE / 'docs' / 'screenshots' / 'm3-stage-b'
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(('ok   - ' if ok else 'FAIL - ') + name + (f'  [{detail}]' if detail and not ok else ''))


class Driver:
    def __init__(self):
        self.base = f'http://127.0.0.1:{DRIVER_PORT}'
        self.proc = subprocess.Popen(['geckodriver', '--port', str(DRIVER_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.call('GET', '/status'); break
            except Exception:
                time.sleep(0.2)
        caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'unhandledPromptBehavior': 'ignore',
                                                'moz:firefoxOptions': {'args': ['-headless'],
                                                    # the comparison player starts on a plain button click;
                                                    # headless has no gesture pipeline, so allow audio autoplay
                                                    'prefs': {'media.autoplay.blocking_policy': 0}}}}}
        ses = self.call('POST', '/session', caps)
        if 'sessionId' not in ses.get('value', {}):
            raise RuntimeError('geckodriver session failed: ' + json.dumps(ses)[:300])
        self.sid = ses['value']['sessionId']

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
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
        r = self.call(method, f'/session/{self.sid}{path}', body if body is not None else ({} if method == 'POST' else None))
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

    def click(self, css):
        el = self.s('POST', '/element', {'using': 'css selector', 'value': css})
        self.s('POST', f"/element/{list(el.values())[0]}/click", {})

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
                            '-vf', 'palettegen=max_colors=136', str(path) + '.pal.png'], check=True)
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path), '-i', str(path) + '.pal.png',
                            '-lavfi', 'paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle',
                            str(path) + '.q.png'], check=True)
            os.replace(str(path) + '.q.png', str(path))
            os.remove(str(path) + '.pal.png')
        return path.stat().st_size

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


HOOK = """
window.__log = [];
const real = window.fetch.bind(window);
window.fetch = (url, opts) => { window.__log.push({url: String(url)}); return real(url, opts); };
window.__hits = (frag) => window.__log.filter(l => l.url.includes(frag)).length;
"""

# real jobs registered through the real job API before the server starts answering;
# the launcher also reproduces web.py's __main__ startup block so the process behaves identically
LAUNCHER = """
import sys, json, threading
sys.path.insert(0, %r); sys.path.insert(0, %r)
import project
from pathlib import Path
project.ROOT = Path(%r)
import web
from http.server import ThreadingHTTPServer
slug, ids = json.loads(%r)
a = web.new_job("enhance", ["upscale", "smooth", "encode"], project=slug, scene=ids[1])
web.reserve(a, [web.scene_key(slug, ids[1])])
web.transition(a, "preparing"); web.activate(a, "upscale"); web.transition(a, "submitted"); web.transition(a, "running")
u = web.new_job("render-draft", ["render"], project=slug, scene=ids[4])
web.reserve(u, [web.scene_key(slug, ids[4])])
web.unknown(u, "lost track: the submit was never answered; ComfyUI may still be executing. The job will not be resubmitted.")
f = web.new_job("render-draft", ["render"], project=slug, scene=ids[5])
web.reserve(f, [web.scene_key(slug, ids[5])])
web.fail(f, "ComfyUI unreachable at http://127.0.0.1:1: <urlopen error Connection refused>")
project.recover_interrupted()
web.sweep_enhance_tmp()
threading.Thread(target=web.reconcile_loop, daemon=True).start()
threading.Thread(target=web.ws_listen, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", %d), web.Handler).serve_forever()
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-compare-'))
    man = make_fixture.build(tmp)
    slug, ids = man['slug'], man['scene_ids']
    launcher = LAUNCHER % (str(HERE), str(HERE / 'tests' / 'fixtures'), str(projects.ROOT), json.dumps([slug, ids]), WEB_PORT)
    web = subprocess.Popen([sys.executable, '-c', launcher], env=dict(os.environ, COMFY_URL='http://127.0.0.1:1'),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1); break
            except Exception: time.sleep(0.2)
        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.js(HOOK)
        d.resize(1440, 900)
        time.sleep(0.5)

        def select(i):
            d.js(f"document.querySelectorAll('#navigator .navitem')[{i}].click(); return 1;")
            time.sleep(0.3)

        # ---------- two separate words: is the scene current, and what is running ----------
        select(0)
        check('scene 1 states its validity ("Selected · settings changed") with no execution claim',
              d.js("return [document.getElementById('scValidity').textContent, !!document.getElementById('scExec')];")
              == ['Selected · settings changed', False])
        select(1)
        run = d.js("""const ex = document.getElementById('scExec');
                      const nav = document.querySelectorAll('#navigator .navitem')[1].querySelector('.navstate').textContent;
                      return {exec: ex && ex.getAttribute('data-exec'), nav: nav, val: document.getElementById('scValidity').textContent};""")
        check('scene 2 running enhance reads "Running · upscale" in inspector and navigator',
              run['exec'] == 'Running · upscale' and 'Running · upscale' in run['nav'] and run['val'] == 'Ready', json.dumps(run))
        select(4)
        legacy = d.js("""return {val: document.getElementById('scValidity').textContent,
                                 exec: document.getElementById('scExec').getAttribute('data-exec')};""")
        check('legacy scene separates "Ready · provenance unavailable" from the unknown job state',
              legacy['val'] == 'Ready · provenance unavailable'
              and legacy['exec'] == 'Unknown · ComfyUI may still be executing', json.dumps(legacy))
        select(5)
        failed = d.js("""return {val: document.getElementById('scValidity').textContent,
                                 exec: document.getElementById('scExec').getAttribute('data-exec')};""")
        check('failed render reads "Failed · renderer unavailable", not a raw traceback',
              failed['val'] == 'Ready · prompt changed' and failed['exec'] == 'Failed · renderer unavailable', json.dumps(failed))

        # ---------- a disabled control never sits next to silence ----------
        select(1)
        why = d.js("""const w = id => { const e = document.getElementById(id); return e && !e.hidden ? e.textContent : null; };
                      return [w('anotherWhy'), w('enhanceWhy'), w('renderWhy'), w('compareWhy'), w('primaryWhy')];""")
        check('every disabled action on the running scene names the same reason',
              why == ['scene has a running job'] * 5, json.dumps(why))
        select(3)
        few = d.js("return [document.getElementById('bCompare').disabled, document.getElementById('compareWhy').textContent];")
        check('a one-take scene explains the disabled comparison',
              few[0] and few[1] == 'this scene has fewer than two takes', json.dumps(few))
        select(4)
        unk = d.js("""const w = id => document.getElementById(id).textContent;
                      return [w('anotherWhy'), w('compareWhy')];""")
        check('an unknown job blocks new work with its reason',
              unk == ['job state unknown - dismiss or reconcile it first'] * 2, json.dumps(unk))

        # ---------- unknown-job notice: honest Reconcile, Dismiss behind a dialog ----------
        notice = d.js("""const n = document.getElementById('unkJob');
                         return n ? [...n.querySelectorAll('b, span')].map(e => e.textContent) : null;""")
        check('unknown-job notice names the job, the missing prompt ids and the reservation',
              notice and notice[0].startswith('Unknown job ') and notice[0].endswith('— ComfyUI may still be executing.')
              and notice[1] == 'ComfyUI prompt ids: none recorded (the submit was never answered).'
              and notice[2].startswith('The scene stays reserved until ComfyUI proves the prompt stopped')
              and bool(d.js("return document.getElementById('unkReconcile') && document.getElementById('unkDismiss');")), json.dumps(notice))
        d.js("document.getElementById('unkReconcile').click(); return 1;")
        told = d.wait("document.getElementById('unkMsg') && document.getElementById('unkMsg').textContent === "
                      "'ComfyUI cannot tell (unreachable, or the submit was never answered): "
                      "the job stays unknown and the scene stays reserved.'", timeout=20)
        check('Reconcile reports honestly that ComfyUI cannot tell, and keeps the notice',
              bool(told) and bool(d.js("return document.getElementById('unkJob')"))
              and d.js("return window.__hits('/reconcile');") == 1)
        SHOTS.mkdir(parents=True, exist_ok=True)
        n = d.screenshot(SHOTS / 'b1-unknown-job-1440x900.png')
        check('screenshot b1-unknown-job-1440x900 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')
        d.js("document.getElementById('unkDismiss').click(); return 1;")
        check('Dismiss opens a native dialog instead of acting silently',
              bool(d.wait("document.getElementById('dismissAsk').open")))
        d.js("document.getElementById('dismissGo').click(); return 1;")
        gone = d.wait("!document.getElementById('unkJob')", timeout=15)
        after = d.js("""return {ask: document.getElementById('dismissAsk').open,
                                exec: document.getElementById('scExec').getAttribute('data-exec'),
                                why: document.getElementById('anotherWhy').textContent,
                                flash: document.getElementById('saveStatus').textContent,
                                hits: window.__hits('/resolve')};""")
        check('Dismiss frees the scene, records the verdict and says no proof was had',
              bool(gone) and not after['ask'] and after['exec'] == 'Unknown job resolved: dismissed'
              and after['why'] == 'ComfyUI not reachable'
              and after['flash'] == 'Job dismissed (no proof): the scene is free again, but the prompt may still be running'
              and after['hits'] == 1, json.dumps(after))

        # ---------- take comparison: the full surface ----------
        select(2)
        d.js("document.getElementById('bCompare').click(); return 1;")
        d.wait("!document.getElementById('cmpWrap').hidden")
        time.sleep(2)   # both videos' metadata (and with it the diff rows) arrive
        cmp = d.js("""return {videos: [...document.querySelectorAll('#cmpWrap video')].map(v => [v.dataset.side, v.preload, !isNaN(v.duration) ? +v.duration : v.duration]),
                            viewer: document.getElementById('viewer').hidden,
                            btn: document.getElementById('bCompare').textContent,
                            sync: document.getElementById('cmpSync').textContent,
                            max: document.getElementById('cmpSeek').max,
                            diffs: [...document.querySelectorAll('#cmpDiff .badge')].map(b => b.textContent)};""")
        check('comparison replaces the single viewer with exactly two metadata-preloading players',
              [v[0] for v in cmp['videos']] == ['A', 'B'] and all(v[1] == 'metadata' and v[2] > 0 for v in cmp['videos']) and cmp['viewer'], json.dumps(cmp['videos']))
        check('the compare button reads as what it now does', cmp['btn'] == 'Close comparison', cmp['btn'])
        check('sync line states the honesty limit verbatim',
              cmp['sync'] == 'both takes start at 0 s · sync approximate (within 0.25 s) · controls span the longer take', cmp['sync'])
        check('one seek bar spans the longer take', cmp['max'] == '7', cmp['max'])
        check('the diff rows list exactly what the two takes were generated with',
              cmp['diffs'] == ['seed 77 vs 33',
                               'loras gate-grain.safetensors ×0.5 vs meadow-style.safetensors ×0.8',
                               'rendered for 7 s vs 6 s',
                               'length 7.0 s vs 6.0 s — A is 1.0 s longer',
                               'B is the enhanced (finished) file'], json.dumps(cmp['diffs']))
        n = d.screenshot(SHOTS / 'b1-compare-1440x900.png')
        check('screenshot b1-compare-1440x900 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')

        d.click('#cmpPlay')
        played = d.wait("[...document.querySelectorAll('#cmpWrap video')].every(v => v.currentTime > 0.3)", timeout=10)
        check('Play runs both takes forward together', bool(played),
              str(d.js("return [...document.querySelectorAll('#cmpWrap video')].map(v => v.currentTime);")))
        check('the bar label follows the real playback state',
              d.js("return document.getElementById('cmpPlay').textContent;") == 'Pause')
        d.js("document.getElementById('cmpPlay').click(); return 1;")   # trusted pause
        d.js("const s = document.getElementById('cmpSeek'); s.value = '1.5'; s.dispatchEvent(new Event('input')); return 1;")
        time.sleep(0.2)
        seek = d.js("return [...document.querySelectorAll('#cmpWrap video')].map(v => v.currentTime);")
        check('the single seek bar drives both players (shorter take clamped to its end)',
              all(abs(t - 1.5) <= 0.3 for t in seek), json.dumps(seek))
        d.js("document.getElementById('cmpAudio').click(); return 1;")
        audio = d.js("""return [document.getElementById('cmpAudio').textContent,
                                [...document.querySelectorAll('#cmpWrap video')].map(v => v.muted)];""")
        check('audio toggle keeps exactly one take audible',
              audio[0] == 'audio: B' and audio[1] == [True, False], json.dumps(audio))

        # pane B is the take not in use: using it must land on disk and re-label the pane
        h0 = d.js("return window.__hits('/star');")
        d.js("""const box = document.querySelector('#cmpWrap [data-meta="B"]');
                [...box.querySelectorAll('button')].find(b => b.textContent === 'Use this take').click(); return 1;""")
        d.wait(f"window.__hits('/star') > {h0}")
        time.sleep(1.2)
        s3 = projects.load(slug)['scenes'][2]
        ref, mode = projects.selection_of(s3)
        c = projects.chosen(s3)
        pane = d.js("""const box = document.querySelector('#cmpWrap [data-meta="B"]');
                       return {badges: [...box.querySelectorAll('.badge')].map(b => b.textContent),
                               btns: [...box.querySelectorAll('button')].map(b => [b.textContent, b.disabled])};""")
        check('Use this take (pane B) picks that take explicitly on disk',
              mode == 'explicit' and c and c.get('seed') == 33, f'{ref} {mode} {c and c.get("seed")}')
        check('the pane re-labels its take as the one in use',
              'pinned' in pane['badges'] and ['in use here', True] in pane['btns'], json.dumps(pane))
        flags = d.js("""return {film: [document.getElementById('filmFlag').hidden, document.getElementById('filmFlag').textContent],
                               anim: [document.getElementById('animFlag').hidden, document.getElementById('animFlag').textContent]};""")
        check('both film buttons now warn the cut would change',
              not flags['film'][0] and 'cut outdated' in flags['film'][1] and 'takes' in flags['film'][1]
              and not flags['anim'][0], json.dumps(flags))
        d.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); return 1;")
        esc = d.wait("""document.getElementById('cmpWrap').hidden && document.getElementById('cmpBar').hidden &&
                        !document.getElementById('viewer').hidden && document.activeElement.id === 'bCompare' &&
                        document.getElementById('bCompare').textContent === 'Compare takes'""", timeout=5)
        check('Escape returns to the single viewer and hands focus back to the compare button', bool(esc),
              str(d.js("return [document.getElementById('cmpWrap').hidden, document.activeElement.id, "
                       "document.getElementById('bCompare').textContent];")))

        # ---------- continuity: changing upstream does not rewrite downstream, and says so ----------
        select(0)
        h1 = d.js("return window.__hits('/star');")
        d.js("""const card = [...document.querySelectorAll('#takeStrip .take')].find(c => c.querySelector('[data-k=bUse]'));
                card.querySelector('[data-k=bUse]').click(); return 1;""")
        d.wait(f"window.__hits('/star') > {h1}")
        time.sleep(1.2)
        select(1)
        cont = d.js("""const cb = document.getElementById('contBox');
                       return {val: document.getElementById('scValidity').textContent,
                               nav: document.querySelectorAll('#navigator .navitem')[1].querySelector('.navstate').textContent,
                               hidden: cb.hidden, text: cb.textContent,
                               regen: !!document.getElementById('contRegen') && document.getElementById('contRegen').disabled};""")
        check('the downstream scene is flagged current-but-outdated, not broken',
              cont['val'] == 'Ready · continuity outdated' and 'continuity outdated' in cont['nav'], json.dumps(cont))
        check('the continuity box says the take is kept and only a regenerate continues from the new one',
              not cont['hidden']
              and 'continued from an earlier take of the previous scene' in cont['text']
              and 'Using another take upstream does not rewrite this scene' in cont['text']
              and 'Regenerate from current take' in cont['text'], json.dumps(cont)[:220])
        check('the regenerate action is withheld while the scene has a running job', cont['regen'])
        n = d.screenshot(SHOTS / 'b1-continuity-outdated-1440x900.png')
        check('screenshot b1-continuity-outdated-1440x900 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')
        # ---------- a brand-new scene: reasons before a prompt, then renderer availability ----------
        d.js("document.getElementById('bAddScene').click(); return 1;")
        d.wait("document.querySelectorAll('#navigator .navitem').length === 7")
        select(6)
        fresh = d.js("""const w = id => { const e = document.getElementById(id); return e && !e.hidden ? e.textContent : null; };
                        return {val: document.getElementById('scValidity').textContent,
                                another: w('anotherWhy'), enhance: w('enhanceWhy'), render: w('renderWhy'),
                                primary: document.getElementById('bPrimary').textContent};""")
        check('a new scene without a prompt says what is missing for each action',
              fresh['val'] == 'nothing generated yet' and fresh['another'] == 'Create a prompt first'
              and fresh['enhance'] == 'no take to enhance' and fresh['render'] == 'Create a prompt first'
              and fresh['primary'] == 'Create prompt', json.dumps(fresh))
        h2 = d.js("return window.__hits('/save');")
        d.js("""const ta = document.getElementById('scPrompt'); ta.value = 'a lantern swings twice';
                ta.dispatchEvent(new Event('input')); return 1;""")
        armed = d.wait(f"""window.__hits('/save') > {h2} &&
                          document.getElementById('anotherWhy').textContent === 'ComfyUI not reachable'""", timeout=8)
        check('once a prompt exists, render reasons switch to renderer availability',
              bool(armed),
              str(d.js("return document.getElementById('anotherWhy').textContent;")))

        # ---------- phone: the comparison view fits 390x844 stacked ----------
        d.resize(500, 930)
        d.go('about:blank')
        d.js("""document.write('<body style="margin:0;background:#101014"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        frame = d.s('POST', '/element', {'using': 'css selector', 'value': '#__f'})
        fid = list(frame.values())[0]
        d.s('POST', '/frame', {'id': frame})
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.js("document.querySelectorAll('#navigator .navitem')[2].click(); return 1;")
        d.js("document.getElementById('bCompare').click(); return 1;")
        phone = d.wait("!document.getElementById('cmpWrap').hidden && "
                       "document.querySelectorAll('#cmpWrap video').length === 2 && "
                       "document.getElementById('cmpSeek').max > 0", timeout=20)
        check('phone: comparison opens with two players and a live seek bar', bool(phone))
        stack = d.js("""window.scrollTo(0, 0);
                        const cw = document.documentElement.clientWidth;
                        const w = document.getElementById('cmpWrap').getBoundingClientRect();
                        const a = document.querySelector('#cmpWrap video[data-side="A"]').getBoundingClientRect();
                        const b = document.querySelector('#cmpWrap video[data-side="B"]').getBoundingClientRect();
                        const bar = document.getElementById('cmpBar').getBoundingClientRect();
                        const seek = document.getElementById('cmpSeek').getBoundingClientRect();
                        return {stacked: b.top >= a.bottom - 1, inView: w.left >= 0 && w.right <= cw + 1,
                                barBelow: bar.top >= a.bottom - 1,
                                barInView: bar.left >= 0 && bar.right <= cw + 1 && seek.width >= 100};""")
        check('phone: the two panes stack inside 390 px with the shared bar fully visible below them',
              stack['stacked'] and stack['inView'] and stack['barBelow'] and stack['barInView'], json.dumps(stack))
        d.js("document.getElementById('cmpWrap').scrollIntoView({block: 'start'}); return 1;")
        time.sleep(0.6)
        d.s('POST', '/frame', {'id': None})
        time.sleep(0.4)
        n = d.element_screenshot(fid, SHOTS / 'b1-compare-390x844.png')
        check('screenshot b1-compare-390x844 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')
        print(f'screenshots -> {SHOTS}')
    finally:
        if d: d.close()
        web.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for r in results if r)
    print(f'{passed}/{len(results)} browser checks passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
