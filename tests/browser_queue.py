"""Browser check of Stage B2: the queue drawer in project.html, the two honest per-job times
("queued for" waiting for ComfyUI, "running for" executing), cancel behind an accessible
dialog, the same real data on /queue and on the single-shot page, and the rule that a poll
must never disturb focus, selection, caret, open panels or video playback.

Same harness as browser_compare.py: headless Firefox over raw WebDriver against the six-scene
ffmpeg fixture; web.py runs in-process on a spare port with COMFY_URL pointed at a closed port.
Seven jobs are registered through the real job API before the first request is answered:
an enhance mid-flight on scene 2 (created 1.2 s before it reached ComfyUI, so "queued for" is
a real waited time), a finished enhance on scene 2 whose take Details can open, a preparing
enhance on scene 3 with no prompt ids (so Cancel proves the stop through the real route),
a running render on scene 4 carrying fake prompt ids (so Cancel hits the closed port and must
answer with its warning instead of pretending), the legacy unknown on scene 5, the failed
render on scene 6, and one preparing job belonging to another film. Nothing is simulated in
the page: every string is project.html / queue.html / index.html reading real /api state.

Screenshots of the drawer at 1440x900 and 390x844 land in docs/screenshots/m3-stage-b/.
Needs `geckodriver` and `firefox` on PATH. Throwaway projects dir; never touches real projects.
Run: python3 tests/browser_queue.py
"""
import base64, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import project as projects   # noqa: E402
import make_fixture          # noqa: E402

WEB_PORT, DRIVER_PORT = 8208, 4462
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

    def key(self, ch):
        self.s('POST', '/actions', {'actions': [{'type': 'key', 'id': 'k',
                                                 'actions': [{'type': 'keyDown', 'value': ch},
                                                             {'type': 'keyUp', 'value': ch}]}]})

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

AUDIT = """return (() => {
  const out = [];
  const idof = e => e.id ? '#' + e.id : (e.className ? '.' + String(e.className).split(' ')[0] : e.tagName.toLowerCase());
  for (const e of document.querySelectorAll('body *')) {
    if (e.offsetParent === null) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(e);
    if (cs.display === 'inline') continue;
    if (cs.overflowX === 'visible' && e.scrollWidth > e.clientWidth + 1 && e.textContent.trim())
      out.push('clipped-text:' + idof(e));
  }
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) out.push('doc-hscroll');
  return out;
})()"""

# colour must never be the whole message: every visible badge/word carries text, and visible
# text sits on its effective background at >= 4.5:1 (WCAG normal text) with control borders
# at >= 3:1 in the dark comparison strip the fix targeted.
CONTRAST = """return (() => {
  const out = [];
  const lum = p => { const f = c => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
                     return 0.2126 * f(p[0]) + 0.7152 * f(p[1]) + 0.0722 * f(p[2]); };
  const parse = s => { const m = s.match(/[\\d.]+/g); return m ? m.slice(0, 3).map(Number) : null; };
  const alpha = s => { const m = s.match(/[\\d.]+/g); return m && m.length > 3 ? +m[3] : 1; };
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b); const x = Math.max(l1, l2), y = Math.min(l1, l2); return (x + 0.05) / (y + 0.05); };
  const bgOf = e => { for (let n = e; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (alpha(c) === 0) continue;
      return parse(c); }
    return [255, 255, 255]; };
  const idof = e => e.id ? '#' + e.id : (e.className ? '.' + String(e.className).split(' ')[0] : e.tagName.toLowerCase());
  for (const e of document.querySelectorAll('body *')) {
    if (e.offsetParent === null) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || +cs.opacity < 0.9) continue;
    const txt = [...e.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim()).map(n => n.textContent).join(' ').trim();
    if (!txt) continue;
    const v = ratio(parse(cs.color), bgOf(e));
    if (v < 4.4) out.push('text ' + idof(e) + ' ' + v.toFixed(2) + ' "' + txt.slice(0, 26) + '"');
  }
  for (const e of document.querySelectorAll('.cmpMeta .btn, .cmpMeta .badge')) {
    if (e.offsetParent === null) continue;
    const cs = getComputedStyle(e);
    const b = parse(cs.borderTopColor);
    if (!b) continue;
    const v = ratio(b, bgOf(e));
    if (v < 2.9) out.push('border ' + idof(e) + ' ' + v.toFixed(2));
  }
  return out;
})()"""


def flat(d):
    return d.js("return [document.documentElement.scrollWidth, document.documentElement.clientWidth];")


LAUNCHER = """
import sys, json, threading, time
sys.path.insert(0, %r); sys.path.insert(0, %r)
import project
from pathlib import Path
project.ROOT = Path(%r)
import web
from http.server import ThreadingHTTPServer
slug, ids = json.loads(%r)
p0 = project.load(slug)
run1 = p0['scenes'][1]['takes'][0]['run']
g = web.new_job("enhance", ["upscale", "smooth", "encode"], project=slug, scene=ids[1])
web.reserve(g, [web.scene_key(slug, ids[1])])
web.transition(g, "preparing"); web.activate(g, "upscale")
web.transition(g, "submitted"); web.transition(g, "running")
g['take'] = {"run": run1}
web.finish(g)                       # scene 2 is free again before the next job books it
a = web.new_job("enhance", ["upscale", "smooth", "encode"], project=slug, scene=ids[1])
web.reserve(a, [web.scene_key(slug, ids[1])])
web.transition(a, "preparing"); web.activate(a, "upscale")
time.sleep(1.2)          # a real wait before ComfyUI picked it up: "queued for" has a value
web.transition(a, "submitted"); web.transition(a, "running")
p = web.new_job("enhance", ["upscale", "smooth"], project=slug, scene=ids[2])
web.reserve(p, [web.scene_key(slug, ids[2])])
web.transition(p, "preparing")
c = web.new_job("render-draft", ["render"], project=slug, scene=ids[3])
web.reserve(c, [web.scene_key(slug, ids[3])])
web.transition(c, "preparing"); web.activate(c, "render")
web.transition(c, "submitted"); web.transition(c, "running")
c['prompt_ids'] = ["ghost-1"]                 # ComfyUI verifiably holds it: cancel must ask it
u = web.new_job("render-draft", ["render"], project=slug, scene=ids[4])
web.reserve(u, [web.scene_key(slug, ids[4])])
web.unknown(u, "lost track: the submit was never answered; ComfyUI may still be executing. The job will not be resubmitted.")
f = web.new_job("render-draft", ["render"], project=slug, scene=ids[5])
web.reserve(f, [web.scene_key(slug, ids[5])])
web.fail(f, "ComfyUI unreachable at http://127.0.0.1:1: <urlopen error Connection refused>")
o = web.new_job("enhance", ["upscale"], project="other-film", scene="zz1")
web.reserve(o, [web.scene_key("other-film", "zz1")])
web.transition(o, "preparing")
project.recover_interrupted()
web.sweep_enhance_tmp()
threading.Thread(target=web.reconcile_loop, daemon=True).start()
threading.Thread(target=web.ws_listen, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", %d), web.Handler).serve_forever()
"""

NOTE = 'Jobs live in memory; restarting the director forgets them (ComfyUI may still finish them)'


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-queue-'))
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
        got4 = d.wait("document.getElementById('queueCount').textContent === '3'")
        check('the Queue button counts only the three live jobs in text (unknown/finished are not live)', bool(got4),
              d.js("return document.getElementById('queueCount').textContent;"))

        # ---------- drawer: keyboard open, focus trap, Escape restores focus ----------
        d.js("document.getElementById('openQueue').focus(); return 1;")
        d.key('\ue007')   # a real Enter on the focused Queue button
        opened = d.wait("document.getElementById('queueDrawer').open", timeout=5)
        check('Enter on the focused Queue button opens the drawer', bool(opened))
        trapped = []
        for _ in range(8):
            d.key('\ue004')   # a real Tab
            trapped.append(d.js("""const a = document.activeElement;
                if (document.getElementById('queueDrawer').contains(a)) return 'in:' + (a.textContent || a.tagName).slice(0, 12);
                return 'out:' + (a === document.body ? 'body' : a.id || a.tagName);"""))
        check('Tab cycles inside the open drawer; the rest of the page stays inert',
              all(t.startswith('in:') for t in trapped), json.dumps(trapped))
        d.key('\ue00c')   # a real Escape
        closed = d.wait("!document.getElementById('queueDrawer').open", timeout=5)
        check('Escape closes the drawer and returns focus to the Queue button, never the body',
              bool(closed) and d.js("return document.activeElement === document.getElementById('openQueue');"))
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.getElementById('queueDrawer').open")
        rows = d.js("""return [...document.querySelectorAll('#queueMine .qrow')].map(r => ({
                          kind: r.querySelector('.qkind').textContent,
                          state: r.querySelector('.qstate').textContent,
                          time: r.querySelector('.qtime').textContent,
                          btns: [...r.querySelectorAll('button')].map(b => b.textContent)}));""")
        byk = {(r['kind'], r['state'].split(' · ')[0]): r for r in rows}
        states = set(s for _k, s in byk)
        check('the drawer lists all six of this film\'s jobs (live first, then finished/failed/unknown)',
              len(rows) == 6, json.dumps([r['state'] for r in rows]))
        check('states are carried by words, not colours: each row spells its state',
              {'running', 'preparing', 'state unknown', 'finished', 'failed'} <= states,
              json.dumps(sorted(states)))
        check('the preparing job says it has not started running yet',
              byk[('enhance', 'preparing')]['time'].endswith('not started'), byk[('enhance', 'preparing')]['time'])

        def times(key):
            t = byk[key]['time']
            m = re.match(r'^queued for ((?:\d+m \d+s|\d+s)) · running for ((?:\d+m \d+s|\d+s))$', t)
            return (m.group(1), m.group(2)) if m else None
        at = times(('enhance', 'running'))
        check('the enhance scene 2 row shows two separate times: "queued for" then "running for"',
              at is not None and at[0] == '1s', byk[('enhance', 'running')]['time'])
        check('the wait before ComfyUI and the time executing are not conflated',
              byk[('enhance', 'preparing')]['time'].startswith('queued for ') and
              'not started' in byk[('enhance', 'preparing')]['time'])
        check('the finished job keeps both times after it is over', times(('enhance', 'finished')) is not None,
              byk[('enhance', 'finished')]['time'])
        check('the restart honesty note sits in the drawer verbatim',
              d.js("return [...document.querySelectorAll('#queueDrawer .qnote')].map(e => e.textContent);") == [NOTE],
              str(d.js("return document.querySelector('#queueDrawer .qnote').textContent;")))
        check('jobs from another film collapse behind their own count',
              d.js("""const w = document.getElementById('queueOthersWrap');
                      return !w.hidden && !w.open &&
                             document.getElementById('queueOthersSum').textContent === 'Jobs in other films (1)' &&
                             document.querySelectorAll('#queueOthers .qrow').length === 1;"""))
        unk = byk[('render-draft', 'state unknown')]
        check('the unknown job offers Reconcile and Dismiss in the drawer, and names the missing prompts',
              'Reconcile' in unk['btns'] and 'Dismiss…' in unk['btns'] and
              'ComfyUI may still be executing' in d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                        .find(r => r.textContent.includes('state unknown')); return r.textContent;"""),
              json.dumps(unk))
        check('the finished job on scene 2 offers Details to open its take', 'Details' in byk[('enhance', 'finished')]['btns'],
              json.dumps(byk[('enhance', 'finished')]))
        au = d.js(AUDIT)
        check('DOM audit with the drawer open: no clipped visible text, no sideways page', au == [], json.dumps(au))
        fl = flat(d)
        check('the drawer does not push the page sideways at 1440x900', fl[0] <= fl[1], json.dumps(fl))

        # ---------- keyboard: the listbox moves without a mouse ----------
        d.js("document.getElementById('closeQueue').click(); return 1;")
        nav = d.js("""const it = document.querySelectorAll('#navigator .navitem');
                      it[0].focus();
                      return {n: it.length, tab: [...it].filter(x => x.tabIndex === 0).length,
                              sel: [...it].findIndex(x => x.getAttribute('aria-selected') === 'true'),
                              ti: it[0].getAttribute('role')};""")
        check('navigator items are options and exactly one is tab-reachable',
              nav['n'] == 6 and nav['tab'] == 1 and nav['sel'] == 0 and nav['ti'] == 'option', json.dumps(nav))
        d.js("""document.querySelectorAll('#navigator .navitem')[0]
                  .dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true}));
                return 1;""")
        time.sleep(0.4)
        down = d.js("""const it = document.querySelectorAll('#navigator .navitem');
                       return {sel: [...it].findIndex(x => x.getAttribute('aria-selected') === 'true'),
                               focus: [...it].indexOf(document.activeElement),
                               say: document.getElementById('navSay').textContent,
                               val: document.getElementById('scValidity').textContent};""")
        check('ArrowDown moves selection and viewer to the next scene and announces it',
              down['sel'] == 1 and down['focus'] == 1 and 'Running on' in down['say'] and 'Ready' in down['say'],
              json.dumps(down))
        d.js("""document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true, cancelable: true}));
                return 1;""")
        time.sleep(0.4)
        end = d.js("""const it = document.querySelectorAll('#navigator .navitem');
                      return [...it].findIndex(x => x.getAttribute('aria-selected') === 'true');""")
        check('End jumps selection to the last scene', end == 5, str(end))

        # ---------- Tab order follows the visual order of the page ----------
        select(0)
        d.js("document.getElementById('openQueue').focus(); return 1;")
        toks = ['queue']
        for _ in range(60):
            d.key('\ue004')
            toks.append(d.js("""const a = document.activeElement;
                if (!a || a === document.body) return 'body';
                if (a.id === 'openQueue') return 'queue';
                if (a.closest && a.closest('#navigator')) return 'nav';
                if (a.id === 'viewer') return 'viewer';
                if (a.closest && a.closest('#takeStrip')) return 'take';
                if (a.id === 'scPrompt') return 'prompt';
                return '.';"""))
            if 'prompt' in toks: break
        seen = [t for t in toks if t != '.' and t != 'body']
        order = lambda a, b: a in seen and b in seen and seen.index(a) < seen.index(b)
        check('Tab follows visual order: header queue button, navigator, viewer, take strip, inspector prompt',
              order('queue', 'nav') and order('nav', 'take') and order('take', 'prompt'), json.dumps(seen))

        # ---------- a take card answers Enter like a real control ----------
        select(0)   # scene 1: two takes (the first one pinned)
        strip = d.js("""return {n: document.querySelectorAll('#takeStrip .take').length,
                                 heads: [...document.querySelectorAll('#takeStrip .take')].map(t => t.textContent.slice(0, 20)),
                                 sel: document.querySelector('#navigator .navitem[aria-selected="true"]').textContent.slice(0, 30)};""")
        card = d.js("""const cs = [...document.querySelectorAll('#takeStrip .take')];
                       const c = cs.find(t => (t.getAttribute('aria-label') || '').includes('take 2 of 2'));
                       if (!c) return null;
                       window.__cpos = cs.indexOf(c);
                       c.focus();
                       return {role: c.getAttribute('role'), label: c.getAttribute('aria-label'),
                               pos: window.__cpos, focused: document.activeElement === c};""")
        d.key('\ue007')
        time.sleep(0.5)
        entered = d.js("""const v = document.getElementById('viewerInfo').textContent;
                          return {info: v, prev: [...document.querySelectorAll('#takeStrip .take')].findIndex(c => c.classList.contains('preview'))};""")
        check('Tab lands on a take card and Enter plays take 2/2 in the viewer',
              bool(card) and card['role'] == 'button' and card['label'] == 'Play take 2 of 2 in the viewer' and card['focused']
              and 'take 2/2' in entered['info'] and entered['prev'] == card['pos'],
              json.dumps([strip, card, entered]))

        # ---------- Details: the drawer hands a job's take to the viewer ----------
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.querySelectorAll('#queueMine .qrow').length === 6")
        d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                  .find(r => r.textContent.includes('finished'));
                [...r.querySelectorAll('button')].find(b => b.textContent === 'Details').click(); return 1;""")
        det = d.wait("""!document.getElementById('queueDrawer').open &&
                        document.getElementById('viewerInfo').textContent.includes('Scene 2')""", timeout=8)
        check('Details closes the drawer and shows that job\'s take in the viewer', bool(det),
              str(d.js("return document.getElementById('viewerInfo').textContent;")))

        # ---------- the poll must not disturb what the user is doing ----------
        select(1)
        d.wait("document.getElementById('viewer').readyState >= 1", timeout=15)
        sc0 = d.js("""const v = document.getElementById('viewer');
                window.__v = v; v.currentTime = 1.0;
                const strip = document.getElementById('takeStrip');
                strip.scrollLeft = 60; window.__card = strip.querySelector('.take');
                document.getElementById('techDetails').open = true;
                const nv = document.getElementById('navigator'), isp = document.getElementById('inspector');
                nv.scrollTop = 40; isp.scrollTop = 30;
                window.__sc = [nv.scrollTop, isp.scrollTop, strip.scrollLeft];
                return [window.__sc, v.currentSrc.slice(-30), +v.currentTime.toFixed(1)];""")
        d.js("""const ta = document.getElementById('scPrompt');
                ta.focus(); ta.setSelectionRange(3, 7);
                window.__ta = ta; return 1;""")
        time.sleep(7.0)   # three page polls at least, drawer closed
        stay = d.js("""return {ta: window.__ta === document.getElementById('scPrompt'),
                          active: document.activeElement === window.__ta,
                          sel: [document.activeElement.selectionStart, document.activeElement.selectionEnd],
                          v: window.__v === document.getElementById('viewer'),
                          t: +document.getElementById('viewer').currentTime.toFixed(1),
                          card: window.__card === undefined || window.__card.isConnected,
                          tech: document.getElementById('techDetails').open,
                          scn: [document.getElementById('navigator').scrollTop,
                                document.getElementById('inspector').scrollTop,
                                document.getElementById('takeStrip').scrollLeft]};""")
        check('caret, selection and focus survive the page polls (textarea keeps the caret at 3-7)',
              stay['ta'] and stay['active'] and stay['sel'] == [3, 7], json.dumps(stay))
        check('the watched video is not reloaded or rewound by the polls', stay['v'] and stay['t'] >= 0.9, json.dumps(stay))
        check('open take Details, navigator/inspector/take-strip scrolls persist through the polls',
              stay['card'] and stay['tech'] and stay['scn'] == sc0[0], json.dumps(stay))
        d.js("""document.querySelectorAll('#navigator .navitem')[1].focus();
                window.__nav = document.activeElement; return 1;""")
        time.sleep(3.6)   # one page poll at least
        check('focused navigator item keeps focus through the polls',
              d.js("return document.activeElement === window.__nav;"))
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.querySelectorAll('#queueMine .qrow').length === 6")
        d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                  .find(r => r.textContent.includes('state unknown'));
                window.__qrow = r; return r.querySelector('.qtime').textContent;""")
        time.sleep(4.0)   # two drawer polls at least
        q = d.js("""return {same: window.__qrow.isConnected && window.__qrow ===
                              [...document.querySelectorAll('#queueMine .qrow')].find(r => r.textContent.includes('state unknown')),
                              t: window.__qrow.querySelector('.qtime').textContent};""")
        check('a queue row is never rebuilt while its data is unchanged, yet its times tick',
              q['same'] and q['t'].startswith('queued for'), json.dumps(q))

        # ---------- cancel: a dialog first, and proof or an honest warning after ----------
        d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                  .find(r => r.textContent.includes('preparing'));
                [...r.querySelectorAll('button')].find(b => b.textContent === 'Cancel…').click(); return 1;""")
        ask = d.wait("document.getElementById('cancelAsk').open", timeout=5)
        what = d.js("return document.getElementById('cancelAskWhat').textContent;")
        check('Cancel… opens a native dialog instead of acting silently', bool(ask))
        check('the confirm dialog names the job and says the stop must be proven',
              'enhance' in what and 'once the stop is proven' in what, what)
        d.key('\ue00c')   # real Escape
        escd = d.wait("!document.getElementById('cancelAsk').open && document.getElementById('queueDrawer').open", timeout=5)
        check('Escape leaves the job alone and returns to the open drawer', bool(escd))
        h0 = d.js("return window.__hits('/cancel');")
        d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                  .find(r => r.textContent.includes('preparing'));
                [...r.querySelectorAll('button')].find(b => b.textContent === 'Cancel…').click(); return 1;""")
        d.wait("document.getElementById('cancelAsk').open")
        d.js("document.getElementById('cancelGo').click(); return 1;")
        proved = d.wait("""document.getElementById('queueFlash').textContent === 'Cancelled: the stop is proven.'""", timeout=10)
        crows = d.wait("""[...document.querySelectorAll('#queueMine .qrow')]
                           .some(r => r.querySelector('.qstate').textContent === 'cancelled')""", timeout=15)
        check('confirming Cancel asks the server once and reports "the stop is proven"',
              bool(proved) and d.js("return window.__hits('/cancel');") == h0 + 1,
              str(d.js("return document.getElementById('queueFlash').textContent;")))
        check('the cancelled row then reads "cancelled" in words', bool(crows),
              json.dumps(d.js("return [...document.querySelectorAll('#queueMine .qstate')].map(e => e.textContent);")))
        d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                  .find(r => r.textContent.includes('render-draft') && r.textContent.includes('running'));
                [...r.querySelectorAll('button')].find(b => b.textContent === 'Cancel…').click(); return 1;""")
        d.wait("document.getElementById('cancelAsk').open")
        d.js("document.getElementById('cancelGo').click(); return 1;")
        warn = d.wait("""document.getElementById('queueFlash').textContent ===
                        'ComfyUI did not confirm the stop: the job stays live until the stop is proven'""", timeout=10)
        still = d.js("""const r = [...document.querySelectorAll('#queueMine .qrow')]
                          .find(r => r.textContent.includes('render-draft') && r.textContent.includes('running'));
                        return !!r;""")
        check('cancel without ComfyUI proof says so and does NOT pretend the job is gone',
              bool(warn) and still, str(d.js("return document.getElementById('queueFlash').textContent;")))
        cnt3 = d.wait("document.getElementById('queueCount').textContent === '2'")
        check('the header count follows the real job states', bool(cnt3),
              d.js("return document.getElementById('queueCount').textContent;"))
        check('the live video and the caret are still untouched after two cancelling round-trips',
              d.js("""return document.getElementById('viewer') === window.__v &&
                          document.getElementById('scPrompt') === window.__ta &&
                          window.__ta.selectionStart === 3 && window.__ta.selectionEnd === 7;"""))

        # ---------- status never travels by colour alone ----------
        words = d.js("""const sel = ['.badge', '.chip', '.navstate', '#timeline .tls', '#scExec', '#backend',
                                     '#queueCount', '#queueDrawer .qstate'];
                        return sel.flatMap(s => [...document.querySelectorAll(s)]
                          .filter(e => e.offsetParent !== null)
                          .map(e => [s, e.textContent.trim()])).filter(x => !x[1]);""")
        check('every visible status chip, badge and word line carries text, never colour alone',
              words == [], json.dumps(words))
        cont = d.js(CONTRAST)
        check('contrast in the drawer + workspace: every visible text >= 4.5:1 on its background',
              cont == [], json.dumps(cont))
        d.js("document.getElementById('closeQueue').click(); return 1;")
        select(0)   # scene 1: two takes, no job ever touched it, so comparison is allowed
        d.js("document.getElementById('bCompare').click(); return 1;")
        d.wait("!document.getElementById('cmpWrap').hidden")
        d.wait("[...document.querySelectorAll('#cmpWrap video')].every(v => v.readyState >= 1)", timeout=15)
        d.js("""const vs = [...document.querySelectorAll('#cmpWrap video')];
                vs.forEach(v => { v.currentTime = 1.0; });
                window.__cb = vs; window.__sync = document.getElementById('cmpSync').textContent;
                return vs.length;""")
        time.sleep(7.0)   # project + queue polls at least twice while both boxes are mid-playback
        cmpstay = d.js("""const vs = [...document.querySelectorAll('#cmpWrap video')];
                          return {same: window.__cb.length === vs.length && window.__cb.every((v, i) => v === vs[i]),
                                  t: vs.map(v => +v.currentTime.toFixed(1)),
                                  sync: document.getElementById('cmpSync').textContent === window.__sync};""")
        check('comparison playback survives the polls: both box nodes, their times and the sync line untouched',
              cmpstay['same'] and len(cmpstay['t']) == 2 and all(x >= 0.9 for x in cmpstay['t']) and cmpstay['sync'],
              json.dumps(cmpstay))
        cont2 = d.js(CONTRAST)
        check('contrast in the comparison view: its text >= 4.5:1 and its control borders >= 3:1',
              cont2 == [], json.dumps(cont2))
        d.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); return 1;")
        time.sleep(0.4)

        # ---------- the same data on the /queue page, as a table ----------
        d.go(f'http://127.0.0.1:{WEB_PORT}/queue')
        d.js(HOOK)
        d.wait("document.querySelectorAll('#queueBody tr').length === 7", timeout=15)
        head = d.js("return [...document.querySelectorAll('#queueTable th')].map(t => t.textContent);")
        check('the queue table labels both times as separate columns',
              'Queued for' in head and 'Running for' in head, json.dumps(head))
        check('the restart honesty note sits on /queue verbatim',
              d.js("return [...document.querySelectorAll('.hint')].some(e => e.textContent === " + json.dumps(NOTE) + ");"))
        tq = d.js("""const tr = [...document.querySelectorAll('#queueBody tr')]
                       .find(r => r.children[0].textContent === 'enhance' && r.children[2].textContent.includes('running') && r.children[1].textContent.length > 0);
                     window.__tr = tr;
                     return {q: tr.children[4].textContent, run: tr.children[5].textContent, state: tr.children[2].textContent};""")
        check('the running job row fills both time columns with different labelled values',
              re.match(r'^(\d+m \d+s|\d+s)$', tq['q']) and re.match(r'^(\d+m \d+s|\d+s)$', tq['run']), json.dumps(tq))
        t1 = d.js("return [window.__tr.children[4].textContent, window.__tr.children[5].textContent];")
        time.sleep(1.4)
        t2 = d.js("""return [window.__tr === [...document.querySelectorAll('#queueBody tr')].find(r => r === window.__tr),
                              window.__tr.children[4].textContent, window.__tr.children[5].textContent];""")
        def secs(s):
            m = re.match(r'^(?:(\d+)m )?(\d+)s$', s)
            return 60 * int(m.group(1) or 0) + int(m.group(2)) if m else -1
        check('the table times tick client-side without rebuilding the row',
              t2[0] is True and secs(t2[2]) > secs(t1[1]),
              json.dumps([t1, t2]))
        notstart = d.js("""return [...document.querySelectorAll('#queueBody tr')]
                             .filter(r => r.children[2].textContent.trim() === 'preparing')
                             .map(r => r.children[5].textContent);""")
        check('the preparing job shows "not started" in the running column',
              notstart == ['not started'], json.dumps(notstart))
        d.js("""const tr = [...document.querySelectorAll('#queueBody tr')].find(r => r.children[2].textContent.includes('state unknown'));
                [...tr.querySelectorAll('button')].find(b => b.textContent === 'Reconcile').click(); return 1;""")
        rmsg = d.wait("""[...document.querySelectorAll('#queueBody tr')]
                          .some(r => r.textContent.includes('ComfyUI cannot tell'))""", timeout=15)
        check('Reconcile on /queue answers honestly that ComfyUI cannot tell', bool(rmsg))
        d.js("""const tr = [...document.querySelectorAll('#queueBody tr')].find(r => r.children[2].textContent.includes('state unknown'));
                [...tr.querySelectorAll('button')].find(b => b.textContent === 'Dismiss…').click(); return 1;""")
        dq = d.wait("document.getElementById('askBox').open", timeout=5)
        d.js("document.getElementById('askGo').click(); return 1;")
        dmsg = d.wait("""(() => {
                          const tr = [...document.querySelectorAll('#queueBody tr')].find(r => r.textContent.includes('dismissed'));
                          return tr ? (tr.children[7].textContent + ' ||| ' + tr.children[2].textContent) : null;
                        })()""", timeout=15)
        check('Dismiss… needs a confirm, then records the dismissal in words on the row',
              bool(dq) and isinstance(dmsg, str) and 'dismissed' in dmsg and 'still be running' in dmsg, repr(dmsg))
        gone = d.js("""return [...document.querySelectorAll('#queueBody tr')]
                        .filter(r => r.textContent.includes('dismissed'))
                        .map(r => r.children[7].textContent);""")
        check('a resolved row no longer offers Reconcile/Dismiss',
              bool(gone) and all('Reconcile' not in t and 'Dismiss' not in t for t in gone), json.dumps(gone))

        # ---------- the single-shot page shows the same queue ----------
        d.go(f'http://127.0.0.1:{WEB_PORT}/')
        d.wait("!document.getElementById('queueBox').hidden", timeout=15)
        idx = d.js("""return {rows: document.querySelectorAll('#queueJobs li').length,
                               note: [...document.querySelectorAll('#queueBox .hint')].some(e => e.textContent === arguments[0]),
                               times: [...document.querySelectorAll('#queueJobs .when')].map(e => e.textContent).filter(t => t.includes('queued for')).length,
                               states: [...document.querySelectorAll('#queueJobs li span')].map(e => e.textContent).filter(t => t.includes('preparing')).length};""", NOTE)
        check('the single-shot page lists the live jobs with both labelled times and the same note',
              idx['rows'] >= 5 and idx['note'] and idx['times'] >= 4 and idx['states'] >= 1, json.dumps(idx))

        # ---------- drawer at desktop and phone sizes, for human review ----------
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.resize(1440, 900)
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.querySelectorAll('#queueMine .qrow').length === 6")
        time.sleep(0.6)
        SHOTS.mkdir(parents=True, exist_ok=True)
        n = d.screenshot(SHOTS / 'b2-queue-drawer-1440x900.png')
        check('screenshot b2-queue-drawer-1440x900 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')

        d.resize(500, 930)
        d.go('about:blank')
        d.js("""document.write('<body style="margin:0;background:#101014"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        frame = d.s('POST', '/element', {'using': 'css selector', 'value': '#__f'})
        fid = list(frame.values())[0]
        d.s('POST', '/frame', {'id': frame})
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        d.js("document.getElementById('openQueue').click(); return 1;")
        d.wait("document.getElementById('queueDrawer').open")
        time.sleep(0.6)
        pf = flat(d)
        pa = d.js(AUDIT)
        check('phone 390 with the drawer open: flat page and no clipped visible text',
              pf[0] <= pf[1] and pa == [], json.dumps([pf, pa]))
        fit = d.js("""const dd = document.getElementById('queueDrawer').getBoundingClientRect();
                      const cw = document.documentElement.clientWidth;
                      return dd.left >= 0 && dd.right <= cw + 1 && dd.height <= innerHeight + 1;""")
        check('the drawer fits inside 390 px without running past the screen', bool(fit))
        d.s('POST', '/frame', {'id': None})
        time.sleep(0.4)
        n = d.element_screenshot(fid, SHOTS / 'b2-queue-drawer-390x844.png')
        check('screenshot b2-queue-drawer-390x844 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')

        want = {'b2-queue-drawer-1440x900.png': (1440, 900), 'b2-queue-drawer-390x844.png': (390, 844)}
        for name, (w, h) in want.items():
            raw = (SHOTS / name).read_bytes()
            dims = ((int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big'))
                    if raw[:8] == b'\x89PNG\r\n\x1a\n' else None)
            check(f'pixel audit: {name} is exactly {w}x{h} px', dims == (w, h), str(dims))
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
