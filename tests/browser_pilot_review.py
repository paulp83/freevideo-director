"""Browser check of the pilot review page (pilot.py review-serve): headless Firefox through
geckodriver's raw WebDriver HTTP API.

Rates a clip by click and by keyboard (arrow keys inside a scale), accepts, notes, reloads:
the page must restore everything, save each change to ratings.json, show the progress counter,
never reveal arm/scene/seed, and refuse to serve key.json. The pilot run here is fabricated
(one tiny ffmpeg take per slot); no ComfyUI, model or GPU involved.

    python3 tests/browser_pilot_review.py         (from the repo root)
"""
import json, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import config, pilot   # noqa: E402

WEB_PORT, DRIVER_PORT = 8393, 4456
SECRETS = ('simple', 'director', 'x-one', 'x-two', 'x-three', 'arm', 'key.json', 'sentinel')
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
        self.sid = self.call('POST', '/session', caps)['value']['sessionId']

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def s(self, method, path, body=None):
        r = self.call(method, f'/session/{self.sid}{path}',
                      body if body is not None else ({} if method == 'POST' else None))
        if isinstance(r.get('value'), dict) and 'error' in r['value']:
            raise RuntimeError(r['value'].get('message', r['value']['error']))
        return r.get('value')

    def js(self, script, *args):
        return self.s('POST', '/execute/sync', {'script': script, 'args': list(args)})

    def go(self, url): self.s('POST', '/url', {'url': url})

    def click(self, css):
        eid = list(self.s('POST', '/element', {'using': 'css selector', 'value': css}).values())[0]
        self.s('POST', f'/element/{eid}/click')

    def arrow_right(self):   # geckodriver keyDown/keyUp take a single-code-point `value`
        self.s('POST', '/actions', {'actions': [{'type': 'key', 'id': 'k', 'actions': [
            {'type': 'pause', 'duration': 0}, {'type': 'keyDown', 'value': '\ue014'},
            {'type': 'pause', 'duration': 0}, {'type': 'keyUp', 'value': '\ue014'},
            {'type': 'pause', 'duration': 0}]}]})

    def wait(self, script, timeout=10, *args):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js('return (' + script + ')', *args)
            if v: return v
            time.sleep(0.15)
        return None

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


def http(method, path, obj=None):
    url = f'http://127.0.0.1:{WEB_PORT}{path}'
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-pilot-browser-'))
    srv = None
    d = None
    try:
        # a fabricated 12-slot run: one tiny real ffmpeg take reused for every slot
        take = tmp / 'take.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                        '-c:v', 'libx264', '-c:a', 'aac',
                        '-metadata', 'title=SENTINEL-TAKE-META', '-shortest', str(take)],
                       check=True, capture_output=True)
        scenes = {'kind': 'freevideo-pilot-scenes', 'schema': 1, 'fixtures': {}, 'scenes': [
            {'id': 'x-one', 'title': 'One', 'seconds': 6, 'refs': [], 'private_variant': None,
             'idea': 'idea text one sentinel', 'why': 'w'},
            {'id': 'x-two', 'title': 'Two', 'seconds': 6, 'refs': [], 'private_variant': None,
             'idea': 'idea text two sentinel', 'why': 'w'},
            {'id': 'x-three', 'title': 'Three', 'seconds': 6, 'refs': [], 'private_variant': None,
             'idea': 'idea text three sentinel', 'why': 'w'}]}
        (tmp / 'scenes.json').write_text(json.dumps(scenes))
        pilot.PILOT_ROOT = tmp / 'pilot-runs'
        code = pilot.main(['plan', '--id', 'br', '--scenes', str(tmp / 'scenes.json'),
                           '--fixture-dir', str(tmp), '--seeds', '5,7', '--order-seed', '3',
                           '--width', '64', '--height', '48'])
        check('plan made the run', code == 0)
        m = pilot.load_manifest('br')
        for slot in m['slots'].values():
            slot['render'] = {'state': 'finished', 'job_id': 'j', 'prompt_ids': [],
                              'correlation': None, 'submitted_at': 0, 'hold_seconds': 0,
                              'wall_seconds': 1.0, 'started_at': None, 'ended_at': None,
                              'backend_seconds': None, 'queue_wait_seconds': None,
                              'take': str(take), 'error': None, 'at': ''}
        pilot.save_manifest(m)
        code = pilot.main(['review-prepare', '--id', 'br', '--seed', '7'])
        check('review-prepare built the clips', code == 0)

        srv = pilot.review_server('br', '127.0.0.1', WEB_PORT)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        st, _ = http('GET', '/key.json')
        check('key.json is not served', st == 404, str(st))
        st, _ = http('GET', '/clip-01.mp4')
        check('clips are served', st == 200, str(st))

        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/')
        d.wait('document.querySelectorAll("video").length === 12')
        n_videos = d.js('return document.querySelectorAll("video").length')
        n_fields = d.js('return document.querySelectorAll(\'input[name*="|"]\').length')
        check('page shows 12 clips', n_videos == 12, str(n_videos))
        check('5 criteria x 5 scores + verdict per clip', n_fields == 12 * (5 * 5 + 2),
              str(n_fields))
        check('progress starts empty',
              'takes fully rated: 0 / 12' in d.js('return document.body.innerText'))

        # click a score; the status span must say saved and the log must grow
        d.click('input[name="clip-01|action"][value="4"]')
        check('click saves', d.wait('document.getElementById("clip-01|status")'
                                    '.textContent === "saved"'))
        # keyboard: focus a scale, ArrowRight moves 2 -> 3
        d.js('document.querySelector(\'input[name="clip-01|motion"][value="2"]\').focus()')
        d.arrow_right()
        check('arrow key changes the scale',
              d.js('return document.querySelector('
                   '\'input[name="clip-01|motion"]:checked\').value') == '3')

        # finish clip-01: remaining scores + accept + a note
        for crit in ('identity', 'cinematography', 'audio'):
            d.click(f'input[name="clip-01|{crit}"][value="5"]')
        d.click('input[name="clip-01|verdict"][value="accepted"]')
        eid = list(d.s('POST', '/element', {'using': 'css selector',
                                            'value': '#clip-01\\|notes'}).values())[0]
        d.s('POST', f'/element/{eid}/value', {'text': 'looked steady'})
        d.js('document.getElementById("clip-01|notes").blur()')   # a real user leaves the field
        d.wait('document.getElementById("clip-01|status").textContent === "saved"')
        check('fully-rated counter', d.wait(
            'document.getElementById("progress").textContent === "takes fully rated: 1 / 12"'))

        # reload: everything comes back from the server
        d.go(f'http://127.0.0.1:{WEB_PORT}/')
        d.wait('document.querySelector(\'input[name="clip-01|verdict"]'
               '[value="accepted"]:checked\')')
        state = d.js('''
            var g = n => { const r = document.querySelector('input[name="' + n + '"]:checked');
                           return r ? r.value : null; };
            return [g('clip-01|action'), g('clip-01|motion'), g('clip-01|verdict'),
                    document.getElementById('clip-01|notes').value];''')
        check('reload restores ratings', state == ['4', '3', 'accepted', 'looked steady'],
              str(state))

        # ratings on disk: append-only log, one entry per change
        doc = json.loads((pilot.PILOT_ROOT / 'br' / 'ratings.json').read_text())
        check('ratings.json logged every change', len(doc['log']) >= 7, str(len(doc['log'])))
        check('last record is current', doc['log'][-1]['verdict'] == 'accepted')

        # leak scan: what the browser actually received
        html = d.js('return document.documentElement.outerHTML').lower()
        text = d.js('return document.body.innerText').lower()
        for word in SECRETS:
            check(f'no {word!r} in page', word not in html and word not in text)
        check('no arm labels in headings',
              d.js('return [...document.querySelectorAll("h2")].every(h => /^Take \\d+ of 12 '
                   '\\(clip-\\d\\d\\)$/.test(h.textContent.trim()))'))
    finally:
        if d: d.close()
        if srv:
            srv.shutdown(); srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = results.count(False)
    print(f'{len(results) - failed}/{len(results)} checks passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
