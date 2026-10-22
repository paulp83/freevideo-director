#!/usr/bin/env python3
"""Regenerate the docs screenshots (docs/ui.png, docs/film-editor.png, docs/queue.png).

Headless Firefox through geckodriver (same raw-W3C pattern as the browser_* suites) over
the fixture film from tests/fixtures/make_fixture.py -- illustrative media, no user
data. Each shot is 1280x900 and kept under 400 KB. Needs `geckodriver`, `firefox` and
`ffmpeg` on PATH. Run from the repo root:  python3 tests/make_doc_screenshots.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'fixtures'))
import leak_guard          # noqa: E402
import project as projects  # noqa: E402
import make_fixture        # noqa: E402

WEB_PORT, STUB_PORT, DRIVER_PORT = 8227, 8228, 4477
DOCS = REPO / 'docs'


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method='POST' if body is not None else 'GET',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


class Driver:
    def __init__(self):
        self.base = f'http://127.0.0.1:{DRIVER_PORT}'
        self.proc = leak_guard.spawn(['geckodriver', '--port', str(DRIVER_PORT)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                http(self.base + '/status')
                break
            except Exception:
                time.sleep(0.2)
        try:
            caps = {'capabilities': {'alwaysMatch': {
                'browserName': 'firefox',
                'moz:firefoxOptions': {'args': ['-headless']}}}}
            self.sid = http(self.base + '/session', caps)['value']['sessionId']
        except Exception:
            leak_guard.stop(self.proc)
            raise

    def s(self, path, body=None):
        r = http(self.base + f'/session/{self.sid}{path}', body if body is not None else {})
        if isinstance(r.get('value'), dict) and 'error' in r['value']:
            raise RuntimeError(r['value'].get('message', r['value']['error']))
        return r.get('value')

    def js(self, script):
        return self.s('/execute/sync', {'script': script, 'args': []})

    def go(self, url):
        self.s('/url', {'url': url})

    def wait(self, script, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js('return (' + script + ')')
            if v:
                return v
            time.sleep(0.2)
        raise RuntimeError(f'timed out waiting for: {script}')

    def shot(self, path):
        png = base64.b64decode(http(self.base + f'/session/{self.sid}/screenshot')['value'])
        path.write_bytes(png)
        if path.stat().st_size > 400 * 1024:          # flat UI + colourful video frames: quantise
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path),
                            '-vf', 'palettegen=max_colors=136', str(path) + '.pal.png'], check=True)
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(path),
                            '-i', str(path) + '.pal.png',
                            '-lavfi', 'paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle',
                            str(path) + '.q.png'], check=True)
            os.replace(str(path) + '.q.png', str(path))
            os.remove(str(path) + '.pal.png')
        return path.stat().st_size


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-docshots-'))
    man = make_fixture.build(tmp)                    # sets projects.ROOT; real ffmpeg media
    slug = man['slug']
    stub = leak_guard.spawn([sys.executable, str(HERE / 'stub_comfy.py'),
                             '--port', str(STUB_PORT), '--root', str(tmp / 'comfy-output')],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; project.ROOT = Path(%r); '
                'sys.argv = ["web.py", "--port", "%d"]; import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(REPO), str(projects.ROOT), WEB_PORT, str(REPO / 'web.py')))
    web = leak_guard.spawn([sys.executable, '-c', launcher],
                           env=dict(os.environ, COMFY_URL=f'http://127.0.0.1:{STUB_PORT}',
                                    COMFY_ROOT=str(tmp / 'comfy-output'),
                                    DIRECTOR_URL='http://127.0.0.1:1/v1/chat/completions',
                                    JOBS_DB=str(tmp / 'jobs.db')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    shots = []
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        d = Driver()
        d.s('/window/rect', {'width': 1280, 'height': 986})   # chrome correction toward 900 css px

        d.go(f'http://127.0.0.1:{WEB_PORT}/')
        d.wait("document.querySelector('h1') !== null")
        time.sleep(0.6)
        shots.append(('ui.png', d.shot(DOCS / 'ui.png')))

        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6")
        time.sleep(1.5)                                      # thumbnails in
        shots.append(('film-editor.png', d.shot(DOCS / 'film-editor.png')))

        d.go(f'http://127.0.0.1:{WEB_PORT}/queue')
        d.wait("document.body.innerText.includes('Queue')")
        time.sleep(0.6)
        shots.append(('queue.png', d.shot(DOCS / 'queue.png')))
    finally:
        if d is not None:
            try:
                d.s('/delete', {})
            except Exception:
                pass
            leak_guard.stop(d.proc)
        leak_guard.stop(web)
        leak_guard.stop(stub)
    for name, size in shots:
        print(f'{name}: {size} bytes')
    leaks = leak_guard.final_leaks()
    for pid, cmd in leaks:
        print(f'LEAK - leftover process {pid}: {cmd[:100]}')
    oversize = [n for n, s in shots if s >= 400 * 1024]
    if oversize:
        print('OVERSIZE:', oversize)
    return 0 if len(shots) == 3 and not oversize and not leaks else 1


if __name__ == '__main__':
    sys.exit(main())
