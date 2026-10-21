"""Browser check of the new workspace project.html: headless Firefox through geckodriver's
raw WebDriver HTTP API, against the six-scene fixture from tests/fixtures/make_fixture.py
(real media, no ComfyUI, no director model).

Layout is asserted with numbers, not vibes: at >=1100 px the viewer box keeps >=50% of the
viewport width and >=40% of its height; below that the workspace stacks and the navigator
becomes a horizontal strip; at <=600 px every visible tap target is >=44 px tall and text
stays >=14 px. Status comes from words (badges, the "Renderer unavailable" chip), never from
colour alone. Screenshots at five viewport sizes land in docs/screenshots/m3-stage-a/ for
human review.

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
        self.proc = subprocess.Popen(['geckodriver', '--port', str(DRIVER_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.call('GET', '/status'); break
            except Exception:
                time.sleep(0.2)
        caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'unhandledPromptBehavior': 'ignore',
                                                 'moz:firefoxOptions': {'args': ['-headless']}}}}
        self.sid = self.call('POST', '/session', caps)['value']['sessionId']

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
        finally: self.proc.terminate()


HOOK = """
window.__log = [];
const real = window.fetch.bind(window);
window.fetch = (url, opts) => { window.__log.push({url: String(url)}); return real(url, opts); };
window.__hits = (frag) => window.__log.filter(l => l.url.includes(frag)).length;
"""

TARGETS = """
  // visible interactive elements: buttons, inputs, selects, and the clickable strip/nav tiles
  const els = [...document.querySelectorAll('button, input, select, textarea, .navitem, .take')];
  const bad = [], big = [];
  for (const e of els) {
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
    web = subprocess.Popen([sys.executable, '-c', launcher], env=dict(os.environ, COMFY_URL='http://127.0.0.1:1'),
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
        ok = d.wait("document.getElementById('viewer').videoWidth > 0 && document.getElementById('viewer').readyState >= 1", timeout=20)
        check('viewer loads metadata of the in-use take', bool(ok),
              str(d.js("const v=document.getElementById('viewer'); return [v.readyState, v.videoWidth];")))

        # ---------- desktop grid ----------
        for (w, h) in [(1920, 1080), (1440, 900), (1100, 900)]:
            d.resize(w, h)
            m = d.js("""
              const vw = window.innerWidth, vh = window.innerHeight;
              const v = document.getElementById('viewer').getBoundingClientRect();
              const nav = document.getElementById('navigator'), insp = document.getElementById('inspector');
              return {vw, vh, w: v.width, h: v.height,
                      hscroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
                      navdir: getComputedStyle(nav).flexDirection,
                      inspScroll: insp.scrollHeight > insp.clientHeight};""")
            tag = f'{w}x{h}'
            check(f'{tag}: viewer >= 50% of viewport width', m['w'] >= 0.5 * m['vw'], f"{m['w']:.0f} vs {m['vw']}")
            check(f'{tag}: viewer >= 40% of viewport height', m['h'] >= 0.4 * m['vh'], f"{m['h']:.0f} vs {m['vh']}")
            check(f'{tag}: no horizontal document scroll', m['hscroll'])
            check(f'{tag}: navigator is a vertical column', m['navdir'] == 'column', m['navdir'])
        d.resize(1100, 600)          # short desktop window: the inspector column must overflow here
        d.js("document.getElementById('promptBox').open = true; return 1;")
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
        chip = d.js("return document.getElementById('backend').textContent;")
        check('backend chip reports the renderer probe in words', chip.startswith('Renderer unavailable'), chip)
        d.js("""document.querySelectorAll('#navigator .navitem')[4].click();""")
        d.wait("""[...document.querySelectorAll('#scFlags .badge')].some(b => b.textContent.includes('legacy'))""")
        flags = d.js("""return [...document.querySelectorAll('#scFlags .badge')].map(b => b.textContent);""")
        check('legacy scene says "legacy - provenance unavailable" in words',
              any('legacy - provenance unavailable' in f for f in flags), str(flags))
        d.js("document.querySelectorAll('#navigator .navitem')[0].click();")
        d.wait("document.querySelectorAll('#takeStrip .take').length === 2")
        st = d.js("return {text: document.getElementById('scStatus').textContent, pinned: [...document.querySelectorAll('#takeStrip .badge')].map(b => b.textContent)};")
        check('status line names what is in use and how it was picked', 'in use' in st['text'] and any('pinned' in b for b in st['pinned']), str(st))

        # ---------- takes: lazy media, pinned selection, Stage B placeholder ----------
        pre = d.js("""return [...document.querySelectorAll('#takeStrip video')].map(v => v.preload);""")
        check('take strip videos preload none', all(p == 'none' for p in pre), str(pre))
        check('viewer video preloads metadata',
              d.js("return document.getElementById('viewer').preload;") == 'metadata')
        cmp = d.js("return [document.getElementById('bCompare').disabled, document.getElementById('compareWhy').textContent];")
        check('Compare takes is disabled with its Stage B reason visible', cmp[0] and cmp[1] == 'Stage B', str(cmp))
        d.js("document.querySelector('#takeStrip .take [data-k=bUse]').click();")
        d.wait("window.__hits('/star') > 0")
        check('Use this take pins the take (star)',
              bool(d.wait("[...document.querySelectorAll('#takeStrip .badge')].some(b => b.textContent === '★ in use (pinned)')")))

        # ---------- autosave glue on the new ids ----------
        d.js("""const i = document.getElementById('scAction'); i.value = i.value + ' and the wind drops.';
                i.dispatchEvent(new Event('input'));""")
        check('typing in the inspector reaches a /save request', bool(d.wait("window.__hits('/save') > 0", timeout=6)))
        saved = d.wait("document.getElementById('saveStatus').textContent === 'Saved'", timeout=8)
        check('save status flips to Saved', bool(saved), str(d.js("return document.getElementById('saveStatus').textContent;")))

        # ---------- one take, one provenance fetch ----------
        before = d.js("return window.__hits('/take?id=');")
        d.js("document.querySelector('#takeStrip .take [data-k=bDetails]').click();")
        d.wait("!document.getElementById('techBox').hidden")
        rows = d.js("""const rows = {};
            for (const r of document.querySelectorAll('#techBox .trow'))
                rows[r.querySelector('b').textContent] = r.querySelector('span').textContent;
            return rows;""")
        check('Details shows the recorded provenance', rows.get('provenance') == 'recorded' and rows.get('take id'), str(rows)[:200])
        check('provenance is fetched exactly once', d.js("return window.__hits('/take?id=');") == before + 1)
        time.sleep(4)   # a poll cycle must not refetch or close the open panel
        check('a poll does not refetch provenance', d.js("return window.__hits('/take?id=');") == before + 1
              and not d.js("return document.getElementById('techBox').hidden;"))
        d.js("document.querySelector('#takeStrip .take [data-k=bDetails]').click();")
        check('Details toggles closed', d.js("return document.getElementById('techBox').hidden;"))

        # ---------- stacked tablet: navigator becomes a strip ----------
        for (w, h) in [(1024, 768), (768, 1024)]:
            d.resize(w, h)
            m = d.js("""const nav = document.getElementById('navigator');
                        const first = document.querySelector('#navigator .navitem').getBoundingClientRect();
                        const second = document.querySelectorAll('#navigator .navitem')[1].getBoundingClientRect();
                        return {navdir: getComputedStyle(nav).flexDirection, side: second.left > first.left,
                                hscroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
                                hdrFirst: document.querySelector('header').getBoundingClientRect().top <= 1,
                                navTop: nav.getBoundingClientRect().top,
                                viewTop: document.getElementById('viewerBox').getBoundingClientRect().top};""")
            tag = f'{w}x{h}'
            check(f'{tag}: navigator is a horizontal strip', m['navdir'] == 'row' and m['side'], str(m))
            check(f'{tag}: header above the viewer, no horizontal scroll', m['hdrFirst'] and m['viewTop'] >= m['navTop'] and m['hscroll'], str(m))

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
        web.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for r in results if r)
    print(f'{passed}/{len(results)} browser checks passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
