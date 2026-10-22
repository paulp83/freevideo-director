"""Browser check of the new workspace project.html: headless Firefox through geckodriver's
raw WebDriver HTTP API, against the six-scene fixture from tests/fixtures/make_fixture.py
(real media, no ComfyUI, no director model).

Layout is asserted with numbers, not vibes: at >=1100 px the viewer box keeps >=50% of the
viewport width and >=40% of its height and starts within 150 px of the top; the header plus the
stage nav together stay <=110 px (<=190 px on a phone) and the next-action button is fully
inside the viewport at every size, wrapping instead of clipping; below 1100 px the workspace
stacks and the navigator becomes a horizontal strip; at <=600 px every visible tap target is
>=44 px tall (the tiny caret inside a stage pill is exempt - the menu button it opens obeys)
and text stays >=14 px. Status comes from words (badges, the "Renderer unavailable" chip),
never from colour alone. Screenshots at five viewport sizes land in docs/screenshots/m3-stage-a/
for human review.

Needs `geckodriver` and `firefox` on PATH. Starts its own web.py on a spare port against a
throwaway projects dir; never touches real projects.
Run: python3 tests/browser_layout.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import project as projects   # noqa: E402
import leak_guard           # noqa: E402
import make_fixture          # noqa: E402

WEB_PORT, DRIVER_PORT = 8205, 4459
SHOTS = HERE / 'docs' / 'screenshots' / 'm3-stage-a'
SIZES = [(1920, 1080), (1440, 900), (1024, 768), (768, 1024), (390, 844)]
results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(('ok   - ' if ok else 'FAIL - ') + name + (f'  [{detail}]' if detail and not ok else ''))


class Driver:
    def __init__(self):
        self.base = f'http://127.0.0.1:{DRIVER_PORT}'
        self.proc = leak_guard.spawn(['geckodriver', '--port', str(DRIVER_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.call('GET', '/status'); break
            except Exception:
                time.sleep(0.2)
        try:                                            # a dead driver must not leak: stop, then fail
            caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'unhandledPromptBehavior': 'ignore',
                                                     'moz:firefoxOptions': {'args': ['-headless']}}}}
            self.sid = self.call('POST', '/session', caps)['value']['sessionId']
        except Exception:
            leak_guard.stop(self.proc)
            raise

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
            except ValueError:                     # geckodriver answers 405 with a bare text body
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
        # geckodriver's window rect includes the headless window chrome (~86 px of height), so
        # correct the rect linearly against the measured viewport instead of trusting the request
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
        import base64
        png = base64.b64decode(self.call('GET', url_path)['value'])
        path.write_bytes(png)
        if path.stat().st_size > 400 * 1024:      # flat UI + colourful video frames: quantise the shot
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
        finally: leak_guard.stop(self.proc)


HOOK = """
window.__log = [];
const real = window.fetch.bind(window);
window.fetch = (url, opts) => { window.__log.push({url: String(url)}); return real(url, opts); };
window.__hits = (frag) => window.__log.filter(l => l.url.includes(frag)).length;
"""

TARGETS = """
  // visible interactive elements: buttons, inputs, selects, and the clickable strip/nav tiles;
  // .stage .caret is exempt - it is the icon inside a pill whose clickable area is the whole
  // pill, and the menu button it opens is measured by the same rule
  const els = [...document.querySelectorAll('button, input, select, textarea, .navitem, .take')];
  const bad = [];
  for (const e of els) {
    if (e.matches('.stage .caret')) continue;
    if (!e.offsetParent || e.disabled && e.offsetHeight === 0) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;            // zero-size decorations do not count
    if (r.height < arguments[0]) bad.push((e.id || e.className || e.tagName) + ' h=' + Math.round(r.height));
  }
  return {bad: bad.slice(0, 8), n: els.filter(e => e.offsetParent).length};
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-layout-'))
    man = make_fixture.build(tmp)            # real ffmpeg media; sets projects.ROOT
    slug, ids = man['slug'], man['scene_ids']
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; project.ROOT = Path(%r); '
                'sys.argv = ["web.py", "--port", "%d"]; import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    # COMFY_URL points at a closed port so the renderer chip is deterministically "unavailable"
    web = leak_guard.spawn([sys.executable, '-c', launcher],
                           # DIRECTOR_URL pinned to a closed port: the prompt chip is
                           # deterministically Down, whatever docker holds on 8144/8078
                           env=dict(os.environ, COMFY_URL='http://127.0.0.1:1',
                                    DIRECTOR_URL='http://127.0.0.1:1/v1/chat/completions',
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
        d.js(HOOK)

        d.resize(1920, 1080)
        lazy = d.js("""const v = document.getElementById('viewer');
                       return [v.readyState, v.videoWidth, v.networkState,
                               v.getAttribute('src') || '', v.getAttribute('poster') || ''];""")
        check('viewer shows the in-use take wired up but lazy: src + poster, zero bytes fetched',
              lazy[0] == 0 and lazy[1] == 0 and lazy[2] == 1 and '/files/' in lazy[3] and lazy[4],
              json.dumps(lazy)[:200])

        # ---------- desktop grid ----------
        for (w, h) in [(1920, 1080), (1440, 900), (1100, 900)]:
            d.resize(w, h)
            m = d.js("""
              const vw = window.innerWidth, vh = window.innerHeight;
              const v = document.getElementById('viewer').getBoundingClientRect();
              const nav = document.getElementById('navigator'), insp = document.getElementById('inspector');
              const na = document.getElementById('nextAction').getBoundingClientRect();
              return {vw, vh, w: v.width, h: v.height, top: v.top,
                      naIn: na.height > 0 && na.left >= 0 && na.right <= vw && na.bottom <= vh,
                      naFit: (e => e.scrollWidth <= e.clientWidth + 1)(document.getElementById('nextAction')),
                      hdrH: document.getElementById('hdr').offsetHeight + document.getElementById('stages').offsetHeight,
                      hscroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
                      navdir: getComputedStyle(nav).flexDirection,
                      inspScroll: insp.scrollHeight > insp.clientHeight};""")
            tag = f'{w}x{h}'
            check(f'{tag}: viewer >= 50% of viewport width', m['w'] >= 0.5 * m['vw'], f"{m['w']:.0f} vs {m['vw']}")
            check(f'{tag}: viewer >= 40% of viewport height', m['h'] >= 0.4 * m['vh'], f"{m['h']:.0f} vs {m['vh']}")
            check(f'{tag}: no horizontal document scroll', m['hscroll'])
            check(f'{tag}: navigator is a vertical column', m['navdir'] == 'column', m['navdir'])
            check(f'{tag}: next action fully inside the viewport', m['naIn'], str(m)[:120])
            check(f'{tag}: next action wraps instead of clipping', m['naFit'], str(m)[:120])
            check(f'{tag}: header + stage nav together <= 110 px', m['hdrH'] <= 110, f"{m['hdrH']} px")
            check(f'{tag}: viewer starts within 150 px of the top', m['top'] <= 150, f"{m['top']:.0f} px")
        d.resize(1100, 600)          # short desktop window: the inspector column must overflow here
        d.js("document.getElementById('scSettings').open = true; return 1;")
        insp = d.js("""const i = document.getElementById('inspector'); i.scrollTop = 500;
                       return {top: i.scrollTop, scroll: i.scrollHeight > i.clientHeight,
                               ovf: getComputedStyle(i).overflowY,
                               vis: document.getElementById('saveStatus').offsetParent !== null};""")
        check('inspector scrolls on its own while the header stays',
              insp['top'] > 0 and insp['ovf'] == 'auto' and insp['vis'], str(insp))
        r = d.js(TARGETS, 32)
        check('desktop: every visible control is >= 32 px tall', not r['bad'], '; '.join(r['bad']))
        fonts = d.js("""
          const px = e => parseFloat(getComputedStyle(e).fontSize);
          const pick = s => { const e = document.querySelector(s); return e ? px(e) : 0; };
          return [px(document.body), pick('.badge'), pick('.navitem .navstate'), pick('#scStatus')];""")
        check('desktop: text stays >= 14 px', all(f >= 14 for f in fonts), str(fonts))

        # ---------- words, not colour: status surfaces ----------
        chips = d.js("return [...document.querySelectorAll('#readiness .rchip')].map(function (c) "
                     "{return c.textContent.replace(/\\s+/g, ' ').trim();});")
        check('readiness chips report all three checks in words',
              chips == ['Prompts Down', 'Video Down', 'Finishing Down'], str(chips))
        d.js("""document.querySelectorAll('#navigator .navitem')[4].click();""")
        d.wait("""[...document.querySelectorAll('#scFlags .badge')].some(b => b.textContent.includes('legacy'))""")
        flags = d.js("""return [...document.querySelectorAll('#scFlags .badge')].map(b => b.textContent);""")
        check('legacy scene says "legacy - provenance unavailable" in words',
              any('legacy - provenance unavailable' in f for f in flags), str(flags))
        d.js("document.querySelectorAll('#navigator .navitem')[0].click();")
        d.wait("document.querySelectorAll('#takeStrip .take').length === 2")
        st = d.js("return {text: document.getElementById('scStatus').textContent, pinned: [...document.querySelectorAll('#takeStrip .badge')].map(b => b.textContent)};")
        check('status line names what is in use and how it was picked', 'in use' in st['text'] and any('pinned' in b for b in st['pinned']), str(st))

        # ---------- inspector: prompt always visible, one primary action ----------
        for (w, h) in [(1440, 900), (1920, 1080)]:
            d.resize(w, h)
            p = d.js("""const i = document.getElementById('inspector');
                        i.querySelectorAll('details').forEach(x => x.open = false);   // initial state
                        i.scrollTop = 0;
                        const ir = i.getBoundingClientRect(), pr = document.getElementById('scPrompt').getBoundingClientRect();
                        const btns = [...i.querySelectorAll('button')].filter(b => {
                          if (!b.offsetParent) return false;
                          const r = b.getBoundingClientRect();
                          return r.top < ir.bottom && r.bottom > ir.top && r.height > 2 && r.width > 2; });
                        const prim = [...i.querySelectorAll('button')].filter(b => b.offsetParent &&
                          ['Create prompt', 'Generate preview', 'Use this take', 'Generate another take'].includes(b.textContent.trim()));
                        return {above: pr.top >= ir.top - 1 && pr.bottom <= ir.bottom + 1,
                                scroll: i.scrollTop, nbtn: btns.length, prim: prim.length,
                                det: [...i.querySelectorAll('details')].map(x => x.id + ':' + x.open).join(','),
                                ids: btns.map(b => b.id || b.textContent.trim()).join(',')};""")
            tag = f'{w}x{h}'
            check(f'{tag}: prompt textarea fully visible without scrolling', p['above'] and p['scroll'] == 0, str(p))
            check(f'{tag}: at most 8 visible buttons in the inspector viewport', p['nbtn'] <= 8, str(p))
            check(f'{tag}: exactly one primary take action visible', p['prim'] == 1, str(p))

        # ---------- takes: posters never videos, pinned selection, Stage B placeholder ----------
        d.resize(1100, 900)
        cards = d.js("""return {vids: document.querySelectorAll('#takeStrip video').length,
                                boxes: document.querySelectorAll('#takeStrip .posterBox').length,
                                imgs: document.querySelectorAll('#takeStrip .posterBox img').length,
                                ov: document.querySelectorAll('#takeStrip .playOv').length};""")
        check('take strip shows zero inline <video> players', cards['vids'] == 0, str(cards))
        check('every take card has a poster and a play affordance',
              cards['boxes'] == 2 and cards['imgs'] == 2 and cards['ov'] == 2, str(cards))
        rows = d.js("""const t = document.querySelector('#takeStrip .take');
                       const bs = [...t.querySelectorAll('.takeActions button')].map(b => Math.round(b.getBoundingClientRect().top));
                       return {uniq: new Set(bs).size, n: bs.length};""")
        check('card action buttons sit on one row at 1100 px', rows['n'] >= 2 and rows['uniq'] == 1, str(rows))
        d.js("""const c = document.querySelector('#takeStrip .take:not(.chosen)');
                window.__before = document.getElementById('viewer').currentSrc || document.getElementById('viewer').src;
                c.click(); return 1;""")
        vs = d.js("return [document.getElementById('viewer').currentSrc || document.getElementById('viewer').src, window.__before];")
        check('clicking a take card plays it in the viewer', vs[0] != vs[1] and '/files/' in vs[0], str(vs)[:160])
        check('viewer stays lazy: preload=none so no mp4 is fetched before playback',
              d.js("return document.getElementById('viewer').preload;") == 'none')
        cmp = d.js("return [document.getElementById('bCompare').disabled, document.getElementById('compareWhy').textContent];")
        check('Compare takes is enabled when the selected scene has two takes', not cmp[0] and cmp[1] == '', str(cmp))
        d.js("document.querySelectorAll('#navigator .navitem')[5].click(); return 1;")
        d.wait("document.getElementById('bCompare').disabled")
        cmp1 = d.js("return [document.getElementById('bCompare').disabled, document.getElementById('compareWhy').textContent];")
        check('Compare takes says why it is disabled on a one-take scene', cmp1[0] and cmp1[1] == 'this scene has fewer than two takes', str(cmp1))
        d.js("document.querySelectorAll('#navigator .navitem')[0].click(); return 1;")
        d.wait("document.querySelectorAll('#takeStrip .take').length === 2")
        d.js("document.querySelector('#takeStrip .take [data-k=bUse]').click();")
        d.wait("window.__hits('/star') > 0")
        check('Use this take pins the take (one badge: pinned)',
              bool(d.wait("[...document.querySelectorAll('#takeStrip .badge')].some(b => b.textContent === 'pinned')")))
        check('exactly one Release to auto control when pinned',
              d.js("""return [...document.querySelectorAll('button')].filter(b =>
                      b.offsetParent && b.textContent.trim() === 'Release to auto').length;""") == 1)

        # ---------- autosave glue on the new ids ----------
        d.js("""const i = document.getElementById('scAction'); i.value = i.value + ' and the wind drops.';
                i.dispatchEvent(new Event('input'));""")
        check('typing in the inspector reaches a /save request', bool(d.wait("window.__hits('/save') > 0", timeout=6)))
        saved = d.wait("document.getElementById('saveStatus').textContent === 'Saved'", timeout=8)
        check('save status flips to Saved', bool(saved), str(d.js("return document.getElementById('saveStatus').textContent;")))

        # ---------- one take, one provenance fetch ----------
        before = d.js("return window.__hits('/take?id=');")
        d.js("document.querySelector('#takeStrip .take [data-k=bDetails]').click();")
        d.wait("!document.getElementById('techDetails').hidden && document.getElementById('techDetails').open")
        rows = d.js("""const rows = {};
            for (const r of document.querySelectorAll('#techBox .trow'))
                rows[r.querySelector('b').textContent] = r.querySelector('span').textContent;
            return rows;""")
        check('Details shows the recorded provenance', rows.get('provenance') == 'recorded' and rows.get('take id'), str(rows)[:200])
        check('provenance is fetched exactly once', d.js("return window.__hits('/take?id=');") == before + 1)
        time.sleep(4)   # a poll cycle must not refetch or close the open panel
        check('a poll does not refetch provenance', d.js("return window.__hits('/take?id=');") == before + 1
              and d.js("return document.getElementById('techDetails').open;"))
        d.js("document.querySelector('#takeStrip .take [data-k=bDetails]').click();")
        check('Details toggles closed', d.js("return !document.getElementById('techDetails').open;"))

        # ---------- stage nav: every stage action is reachable through its menu ----------
        d.resize(1440, 900)
        d.js("document.querySelectorAll('#stageChips .stage')[1].click(); return 1;")
        check('a stage pill opens its action menu',
              bool(d.wait("!!document.getElementById('stageMenu')")))
        mbtn = d.js("""const b = document.querySelector('#stageMenu button');
                       const r = b.getBoundingClientRect();
                       return {txt: b.textContent, dis: b.disabled, title: b.title,
                               in: r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight && r.height >= 32};""")
        check('the menu action is visible, enabled and full-size', not mbtn['dis'] and mbtn['in'], str(mbtn))
        check('the gated menu action names the model as the reason in its title',
              'Prompt model' in mbtn['title'], mbtn['title'][:120])
        m_before = d.js("return window.__hits('/prompts');")
        d.js("document.querySelector('#stageMenu button').click(); return 1;")
        closed = d.wait("document.getElementById('saveStatus').textContent.includes('Prompt model')", timeout=5)
        check('the gated menu action closes the menu and flashes the reason without a request',
              bool(closed) and d.js("return !document.getElementById('stageMenu');")
              and d.js("return window.__hits('/prompts');") == m_before,
              d.js("return document.getElementById('saveStatus').textContent;"))
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')   # clean slate after the flash
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)

        # ---------- stacked tablet: navigator becomes a strip ----------
        for (w, h) in [(1024, 768), (768, 1024)]:
            d.resize(w, h)
            m = d.js("""const nav = document.getElementById('navigator');
                        const first = document.querySelector('#navigator .navitem').getBoundingClientRect();
                        const second = document.querySelectorAll('#navigator .navitem')[1].getBoundingClientRect();
                        const na = document.getElementById('nextAction').getBoundingClientRect();
                        return {navdir: getComputedStyle(nav).flexDirection, side: second.left > first.left,
                                hscroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
                                hdrFirst: document.querySelector('header').getBoundingClientRect().top <= 1,
                                navTop: nav.getBoundingClientRect().top,
                                naIn: na.height > 0 && na.left >= 0 && na.right <= innerWidth && na.bottom <= innerHeight,
                                naFit: (e => e.scrollWidth <= e.clientWidth + 1)(document.getElementById('nextAction')),
                                viewTop: document.getElementById('viewerBox').getBoundingClientRect().top};""")
            tag = f'{w}x{h}'
            check(f'{tag}: navigator is a horizontal strip', m['navdir'] == 'row' and m['side'], str(m))
            check(f'{tag}: header above the viewer, no horizontal scroll', m['hdrFirst'] and m['viewTop'] >= m['navTop'] and m['hscroll'], str(m))
            check(f'{tag}: viewer starts within 260 px of the top', m['viewTop'] <= 260, f"{m['viewTop']:.0f} px")
            check(f'{tag}: next action fully inside the viewport, wrapping not clipping', m['naIn'] and m['naFit'], str(m))

        # ---------- phone: order and tap targets ----------
        # headless firefox clamps real windows to >= 500 css px wide, and geckodriver's
        # window.open popups are unreliable; render the page in an exact 390x844 iframe and
        # run the phone checks with the driver switched into that frame.
        d.resize(500, 930)
        d.go('about:blank')
        d.js("""document.write('<body style="margin:0;background:#101014"><iframe id="__f" ' +
                'src=' + JSON.stringify('http://127.0.0.1:' + arguments[1] + '/project?p=' + arguments[0]) + ' ' +
                'style="width:390px;height:844px;border:0;display:block"></iframe></body>'); return 1;""", slug, WEB_PORT)
        frame = d.s('POST', '/element', {'using': 'css selector', 'value': '#__f'})
        fid = list(frame.values())[0]
        d.s('POST', '/frame', {'id': frame})
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)
        inner = d.js('return [window.innerWidth, window.innerHeight];')
        check('phone: the frame viewport is 390x844', inner == [390, 844], str(inner))
        order = d.js("""const y = s => { const e = document.querySelector(s);
                        window.scrollTo(0, 0); return e.getBoundingClientRect().top + window.scrollY; };
                        return {hdr: y('header'), view: y('#viewerBox'), insp: y('#inspector'), nav: y('#navigator')};""")
        check('phone order: header, viewer, inspector, navigator chips',
              order['hdr'] < order['view'] < order['insp'] < order['nav'], str(order))
        hdr = d.js("""window.scrollTo(0, 0);
                      const kids = [...document.querySelectorAll('#hdr > *')].filter(e =>
                        e.offsetParent && e.getBoundingClientRect().height > 2);
                      const rows = new Set(kids.map(e => Math.round(e.getBoundingClientRect().top / 6) * 6)).size;
                      const na = document.getElementById('nextAction').getBoundingClientRect();
                      const seen = id => document.getElementById(id) && document.getElementById(id).offsetParent !== null;
                      return {rows,
                              vis: seen('openSettings') && seen('readiness') &&
                                   !!document.querySelector('#hdr .btn-shot') &&
                                   [...document.querySelectorAll('#hdr a')].every(a => a.offsetParent !== null),
                              naIn: na.height > 0 && na.left >= 0 && na.right <= innerWidth && na.bottom <= innerHeight,
                              naFit: (e => e.scrollWidth <= e.clientWidth + 1)(document.getElementById('nextAction')),
                              hdrH: document.getElementById('hdr').offsetHeight + document.getElementById('stages').offsetHeight,
                              viewTop: document.getElementById('viewerBox').getBoundingClientRect().top};""")
        check('phone: header controls stay on at most 2 rows', hdr['rows'] <= 2, str(hdr))
        check('phone: settings, queue, single-shot and the backend chip are visible', hdr['vis'], str(hdr))
        check('phone: next action fully inside the viewport, wrapping not clipping', hdr['naIn'] and hdr['naFit'], str(hdr))
        check('phone: header + stage nav together <= 190 px', hdr['hdrH'] <= 190, str(hdr))
        check('phone: viewer starts within 260 px of the top', hdr['viewTop'] <= 260, f"{hdr['viewTop']:.0f} px")
        r = d.js(TARGETS, 44)
        check('phone: every visible tap target is >= 44 px tall', not r['bad'], '; '.join(r['bad']))
        fonts = d.js("""
          const px = e => parseFloat(getComputedStyle(e).fontSize);
          const pick = s => { const e = document.querySelector(s); return e ? px(e) : 0; };
          return [px(document.body), pick('.badge'), pick('.navitem b'), pick('#scStatus')];""")
        check('phone: text stays >= 14 px', all(f >= 14 for f in fonts), str(fonts))
        d.s('POST', '/frame', {'id': None})
        d.js('window.scrollTo(0, 0); return 1;')
        time.sleep(0.6)
        SHOTS.mkdir(parents=True, exist_ok=True)
        n = d.element_screenshot(fid, SHOTS / '390x844.png')
        check('screenshot 390x844 written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6", timeout=20)

        # ---------- screenshots for human review ----------
        SHOTS.mkdir(parents=True, exist_ok=True)
        for (w, h) in SIZES[:-1]:
            d.resize(w, h)
            d.js("window.scrollTo(0, 0); return 1;")
            time.sleep(0.6)
            n = d.screenshot(SHOTS / f'{w}x{h}.png')
            check(f'screenshot {w}x{h} written under 400 KB', 0 < n < 400 * 1024, f'{n} bytes')
        print(f'screenshots -> {SHOTS}')
    finally:
        if d: d.close()
        leak_guard.stop(web)
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for r in results if r)
    leaks = leak_guard.final_leaks()
    for pid, cmd in leaks:
        print(f'LEAK - leftover process {pid}: {cmd[:100]}')
    print(f'{passed}/{len(results)} browser checks passed')
    sys.exit(0 if passed == len(results) and not leaks else 1)


if __name__ == '__main__':
    main()
