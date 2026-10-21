"""Browser check of project.html validity badges and lazy per-take Details: headless Firefox
through geckodriver's raw WebDriver HTTP API.

Badges come from the derived scene.status (no stored fields): directly outdated scenes get text
badges, continuations carry a de-emphasised "would be affected" badge, legacy takes say
"legacy - provenance unavailable", and a poll whose data is unchanged must not touch the take
strip: an open Details panel stays open, keeps its once-fetched payload and refires no request.
Details themselves are fetched lazily per take from GET /api/p/<slug>/take?id=...

Needs `geckodriver` and `firefox` on PATH. Starts its own web.py on a spare port against a
throwaway projects dir; never touches real projects. Takes are hand-written records like
tests/test_status.py: no ComfyUI, no ffmpeg, no director model.
Run: python3 tests/browser_status.py
"""
import hashlib, json, os, secrets, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import project as projects   # noqa: E402

WEB_PORT, DRIVER_PORT = 8203, 4457
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
            return json.loads(e.read())

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

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


HOOK = """
window.__log = [];
const real = window.fetch.bind(window);
window.fetch = (url, opts) => { window.__log.push({url: String(url)}); return real(url, opts); };
window.__badges = () => {
  const out = {};
  for (const card of document.querySelectorAll('#scenes .scene'))
    out[card.id] = {stale: [...card.querySelectorAll('.flags .badge.stale')].map(b => b.textContent),
                    soft: [...card.querySelectorAll('.flags .badge.soft')].map(b => b.textContent)};
  return out;
};"""

DETAILS = """
  const b = document.querySelector(arguments[0]); if (!b) return null;
  const box = b.nextElementSibling, rows = {};
  for (const r of box.querySelectorAll('.trow')) rows[r.querySelector('b').textContent] = r.querySelector('span').textContent;
  return {aria: b.getAttribute('aria-expanded'), hidden: box.hidden, text: box.textContent, rows: rows};
"""


# ---------------------------------------------------------------- the film (mirror of tests/test_status.py)

TAG = 0
TAKES = {}      # scene position -> [(take id, job id, run)]


def record(slug, i, prompt, seed, mode='ref2va', first=None, source=None):
    """Write a jobs/<id>.json snapshot + take record the way render_job does, for scene position i."""
    global TAG
    TAG += 1
    p = projects.load(slug)
    scene = p['scenes'][i]
    tid, jid = secrets.token_hex(8), secrets.token_hex(8)
    run = f'/comfy/output/director/run-{TAG}-{i}'
    refs = []
    if first is None:
        for n, name in enumerate(p['refs']):
            refs.append(dict(projects.store_ref(slug, name,
                                                projects.path(slug, 'refs', name).read_bytes()), index=n))
    firsts = dict(projects.store_ref(slug, 'frame.png', first), source=source) if first else None
    projects.write_job_snapshot(slug, {
        'id': jid, 'kind': 'render-draft', 'created': time.time(), 'project': slug,
        'scene': str(scene['id']), 'take_id': tid, 'preset': 'draft', 'variant': None,
        'mode': mode, 'prompt': projects.ensure_i2va(prompt) if first else prompt,
        'seed': seed, 'width': p['size'][0], 'height': p['size'][1],
        'seconds': scene['seconds'], 'two_pass': False,
        'refs': refs, 'first': firsts, 'loras': p.get('loras'), 'graph': {},
        'director': {'model': None, 'skill_revision': None},
        'instructions': projects.instructions_of(p), 'finishing': None})
    src = dict(source, frame_sha256=firsts['sha256']) if firsts else None
    def apply(proj):
        s = proj['scenes'][i]
        s['seed'] = seed
        s['takes'].append(
            {'id': tid, 'job': jid, 'provenance': 'recorded', 'run': run, 'path': run + '/video.mp4',
             'url': None, 'preset': 'draft', 'seed': seed, 'created': time.time(), 'final': None,
             'final_url': None, 'variant': None, 'source_take': src})
    projects.mutate(slug, apply)
    return tid, jid, run


def add_legacy_take(slug, i, run):
    """A pre-records take: no id, no job, no provenance."""
    def apply(p):
        p['scenes'][i]['takes'].append(
            {'run': run, 'path': run + '/video.mp4', 'url': None, 'preset': 'draft', 'seed': 7,
             'created': time.time(), 'final': None, 'final_url': None, 'variant': None,
             'source_take': None})
    projects.mutate(slug, apply)
    return run


def setup_projects():
    slug = projects.create('Badge film', 'nothing', 30, '16:9',
                           [('cat.png', b'AAA'), ('dog.jpg', b'BBB')])['slug']
    plan = {'title': 'Badge film', 'bible': 'the bible', 'music': '',
            'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                       {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'},
                       {'title': 'Three', 'seconds': 6, 'connect': 'continue', 'action': 'three acts'},
                       {'title': 'Four', 'seconds': 6, 'connect': 'cut', 'action': 'four acts'}]}
    projects.mutate(slug, lambda p: projects.apply_storyboard(p, plan))
    def tune(p):
        p['bible'], p['notes'] = 'the bible', 'the notes'
        p['loras'] = projects.clean_loras([{'name': 'style.safetensors', 'strength': 1.0}])
        for s in p['scenes']:
            s['prompt'] = {'One': 'alpha', 'Two': 'beta', 'Three': 'gamma', 'Four': 'delta'}[s['title']]
    projects.mutate(slug, tune)
    ids = [s['id'] for s in projects.load(slug)['scenes']]
    TAKES[0] = [record(slug, 0, 'alpha', seed=0, mode='ref2va')]
    tid0, _, run0 = TAKES[0][0]
    TAKES[1] = [record(slug, 1, 'beta', seed=8, mode='i2va', first=b'FRAME1',
                       source={'scene': ids[0], 'take': tid0, 'run': run0})]
    _, _, run1 = TAKES[1][0]
    record(slug, 2, 'gamma', seed=9, mode='i2va', first=b'FRAME2',
           source={'scene': ids[1], 'take': TAKES[1][0][0], 'run': run1})
    record(slug, 3, 'delta', seed=4, mode='ref2va')
    return slug, ids


def wait_badges(d, want, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        got = d.js('return window.__badges()')
        if got == want: return got
        time.sleep(0.4)
    return d.js('return window.__badges()')


def badges_of(got, cid):
    return (got.get(cid) or {}).get('stale', [])


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-status-'))
    projects.ROOT = tmp / 'projects'
    slug, ids = setup_projects()
    one, two, three, four = ids
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; project.ROOT = Path(%r); '
                'sys.argv = ["web.py", "--port", "%d"]; import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    web = subprocess.Popen([sys.executable, '-c', launcher],
                           env=dict(os.environ, JOBS_DB=str(tmp / 'jobs.db')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1); break
            except Exception: time.sleep(0.2)
        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/project/classic?p={slug}')
        d.wait("document.querySelectorAll('#scenes .scene').length === 4")
        d.js(HOOK)

        # 1. baseline: everything recorded and matching -- no stale badges; continuations carry the
        #    de-emphasised informational badge naming their upstream scene
        want = {f'scene-{one}': {'stale': [], 'soft': []},
                f'scene-{two}': {'stale': [], 'soft': ['would be affected if One is regenerated']},
                f'scene-{three}': {'stale': [], 'soft': ['would be affected if Two is regenerated']},
                f'scene-{four}': {'stale': [], 'soft': []}}
        got = wait_badges(d, want)
        check('1 baseline: no stale badges, informational badges on the continuation chain', got == want, got)

        # 2. scene prompt edited on disk -> exactly that scene is flagged, by text
        projects.mutate(slug, lambda p: p['scenes'][0].__setitem__('prompt', 'alpha two'))
        got = wait_badges(d, {**want, f'scene-{one}': {'stale': ['prompt changed'], 'soft': []}})
        check('2 a changed prompt badges one scene "prompt changed"',
              badges_of(got, f'scene-{one}') == ['prompt changed']
              and not any(badges_of(got, c) for c in (f'scene-{two}', f'scene-{three}', f'scene-{four}')), got)

        # 3. a reference picture's bytes replaced -> scenes whose take submitted it flag it;
        #    continuations (submitted frames, not refs) do not
        names = projects.load(slug)['refs']
        projects.path(slug, 'refs', names[0]).write_bytes(b'ZZZZ')
        want2 = {**want, f'scene-{one}': {'stale': ['prompt changed', 'references changed'], 'soft': []},
                 f'scene-{four}': {'stale': ['references changed'], 'soft': []}}
        got = wait_badges(d, want2)
        check('3 replaced reference bytes badge the two ref-fed scenes, not the continuations', got == want2, got)
        tips = d.js("""const b = document.querySelector(arguments[0] + ' .flags .badge.stale');
                       return {text: b.textContent, title: b.getAttribute('title')};""", f'#scene-{one}')
        check('3b the stale badge tooltip carries the server sentence and the reason',
              ' — ' in tips['title'] and 'differ from what produced the selected take' in tips['title'], tips)

        # 4. a newer take on One (auto-chosen) makes Two's continuity outdated; pinning the take
        #    Two was actually generated from clears it again
        tid2, jid2, run2 = record(slug, 0, 'alpha two', seed=5, mode='ref2va')
        got = wait_badges(d, {**want2, f'scene-{one}': {'stale': [], 'soft': []},
                              f'scene-{two}': {'stale': ['continuity outdated'],
                                              'soft': ['would be affected if One is regenerated']}})
        check('4a a newer take on the source scene flags the continuation "continuity outdated"',
              badges_of(got, f'scene-{two}') == ['continuity outdated'], got)
        check('4b the take-strip badge says which take is chosen automatically',
              'latest (auto)' in d.js("return document.querySelector(`#scene-" + one + " .take .meta`).textContent"))
        d.click(f'#scene-{one} [data-k="bChoose"][data-t="{TAKES[0][0][0]}"]')
        got = wait_badges(d, {**want2, f'scene-{two}': {'stale': [],
                                              'soft': ['would be affected if One is regenerated']}})
        check('4c pinning the take Two came from clears the continuity badge',
              not badges_of(got, f'scene-{two}'), got)

        # 5. a legacy take (no records) becomes Four's chosen take: no guessed flags, one honest
        #    informational badge, and the earlier ref badge disappears with the take it described
        legacy_run = add_legacy_take(slug, 3, '/comfy/output/director/run-legacy-3')
        got = wait_badges(d, {**want2, f'scene-{four}': {'stale': [], 'soft': ['legacy - provenance unavailable']}})
        check('5 a legacy chosen take badges "legacy - provenance unavailable" without inventing flags',
              got and (badges_of(got, f'scene-{four}') == []
                       and (got[f'scene-{four}']['soft'] == ['legacy - provenance unavailable'])), got)

        # 6. Details load lazily, once, and honestly: take1 shows what it was made with -- seed 0
        #    included -- even though the scene has moved on since
        det_sel = f'#scene-{one} [data-k="bDetails"][data-t="{TAKES[0][0][0]}"]'
        pre = d.js(DETAILS, det_sel)
        check('6a Details start collapsed with aria-expanded=false', pre['aria'] == 'false' and pre['hidden'], pre)
        n0 = d.js("return window.__log.filter(x => x.url.includes('/take?id=')).length")
        d.click(det_sel)
        d.wait(f"!document.querySelector({json.dumps(det_sel)}).nextElementSibling.hidden")
        det = d.js(DETAILS, det_sel)
        n1 = d.js("return window.__log.filter(x => x.url.includes('/take?id=')).length")
        sha_a = hashlib.sha256(b'AAA').hexdigest()[:12]
        check('6b opening Details fires exactly one /take?id= request', n0 == 0 and n1 == 1, (n0, n1))
        check('6c Details carry the recorded provenance', 'recorded' in det['text'], det['text'][:120])
        check('6d Details show the take id, job id, mode and seed 0 as 0',
              det['rows'].get('take id') == TAKES[0][0][0] and det['rows'].get('job id') == TAKES[0][0][1]
              and det['rows'].get('mode') == 'ref2va' and det['rows'].get('seed') == '0', det['rows'])
        check('6e Details list the submitted reference hashes as they were at generation time',
              det['rows'].get('ref 1') == f'{names[0]} sha {sha_a}', det['rows'])
        check('6f Details show the prompt as generated, not the current scene prompt',
              det['rows'].get('prompt') == 'alpha', det['rows'])
        check('6g Details list the LoRAs', 'style.safetensors' in det['rows'].get('loras', ''), det['rows'])

        # 7. polls must not collapse the panel, refire the request or touch the strip
        time.sleep(4.5)   # more than the 3 s poll
        det2 = d.js(DETAILS, det_sel)
        n2 = d.js("return window.__log.filter(x => x.url.includes('/take?id=')).length")
        check('7 a poll with unchanged data keeps Details open and issues no new request',
              det2['aria'] == 'true' and not det2['hidden'] and n2 == 1 and 'seed' in det2['rows'], (det2['aria'], det2['hidden'], n2))

        # 8. a new take rebuilds the strip, yet the open panel is restored from cache, not refetched
        record(slug, 0, 'alpha two', seed=3, mode='ref2va')
        d.wait(f"document.querySelectorAll(`#scene-{one} [data-k=takes] .take`).length === 3")
        det3 = d.js(DETAILS, det_sel)
        n3 = d.js("return window.__log.filter(x => x.url.includes('/take?id=')).length")
        check('8 a take-strip rebuild keeps the open Details panel without refetching',
              det3['aria'] == 'true' and not det3['hidden'] and n3 == 1 and det3['rows'].get('seed') == '0', (det3['aria'], n3))

        # 9. legacy Details answer honestly: no invented settings, nothing claimed as recorded
        leg_sel = f'#scene-{four} [data-k="bDetails"][data-t="{legacy_run}"]'
        d.click(leg_sel)
        d.wait(f"!document.querySelector({json.dumps(leg_sel)}).nextElementSibling.hidden")
        leg = d.js(DETAILS, leg_sel)
        check('9 legacy Details say provenance is unavailable and never claim records',
              'Legacy: provenance unavailable' in leg['text'] and 'provenance recorded' not in leg['text']
              and leg['rows'].get('run') == legacy_run, leg['text'][:160])

        # 10. take videos preload nothing until played
        check('10 take videos carry preload=none',
              all(v == 'none' for v in d.js("""return [...document.querySelectorAll('#scenes .take video')]
                                               .map(v => v.getAttribute('preload'));""")))
    finally:
        if d: d.close()
        web.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    failed = results.count(False)
    print(f'{len(results) - failed}/{len(results)} browser checks passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
