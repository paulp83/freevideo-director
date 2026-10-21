"""Browser check of project.html autosave: headless Firefox through geckodriver's raw WebDriver HTTP API.

Needs `geckodriver` and `firefox` on PATH. Starts its own web.py on a spare port against a throwaway
projects dir; never touches real projects. Run: python3 tests/browser_autosave.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import project as projects   # noqa: E402

WEB_PORT, DRIVER_PORT = 8199, 4455
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

    def new_tab(self):
        h = self.s('POST', '/window/new', {'type': 'tab'})['handle']
        self.s('POST', '/window', {'handle': h}); return h

    def switch(self, h): self.s('POST', '/window', {'handle': h})

    def wait(self, script, timeout=10, *args):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js('return (' + script + ')', *args)
            if v: return v
            time.sleep(0.15)
        return None

    def alert_text(self):
        try: return self.s('GET', '/alert/text')
        except RuntimeError: return None

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()


HOOK = """
window.__log = []; window.__mode = 'pass'; window.__delay = 0;
const real = window.fetch.bind(window);
window.fetch = async (url, opts) => {
  const u = String(url); const isSave = u.endsWith('/save');
  window.__log.push({url: u, body: opts && opts.body});
  if (isSave && window.__mode === 'fail') return new Response(JSON.stringify({error: 'disk on fire'}), {status: 500, headers: {'Content-Type': 'application/json'}});
  if (isSave && window.__delay) await new Promise(r => setTimeout(r, window.__delay));
  return real(url, opts);
};"""


def server_project(slug):
    return json.loads((projects.ROOT / slug / 'project.json').read_text())


def setup_projects():
    out = []
    for name in ('Alpha', 'Beta'):
        p = projects.create(name, 'test film', 8, '16:9', [])
        p['scenes'] = [projects.new_scene('One', 4, action='first'), projects.new_scene('Two', 4, action='second')]
        projects.save(p)
        out.append(p['slug'])
    return out


def type_into(d, css, text):
    el = d.s('POST', '/element', {'using': 'css selector', 'value': css})
    eid = list(el.values())[0]
    d.s('POST', f'/element/{eid}/value', {'text': text})


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-'))
    projects.ROOT = tmp / 'projects'
    alpha, beta = setup_projects()
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; project.ROOT = Path(%r); '
                'sys.argv = ["web.py", "--port", "%d"]; import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    web = subprocess.Popen([sys.executable, '-c', launcher], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1); break
            except Exception: time.sleep(0.2)
        d = Driver()
        url = lambda slug: f'http://127.0.0.1:{WEB_PORT}/project?p={slug}'

        # 1. typing while a save is delayed: the edit made during the request must not be lost
        d.go(url(alpha)); d.wait("document.getElementById('pBible')")
        d.js(HOOK); d.js('window.__delay = 1500')
        type_into(d, '#pBible', 'A'); time.sleep(1.1)       # save fired (700 ms debounce), response still pending
        type_into(d, '#pBible', 'B')
        d.js('window.__delay = 0')
        saved = d.wait("document.getElementById('saveStatus').textContent === 'Saved' && !window.__log.some(x => 0)", 10)
        time.sleep(2.5)                                     # let the follow-up save land
        check('1 edit typed during delayed save survives', server_project(alpha).get('bible') == 'AB', server_project(alpha).get('bible'))
        check('1 status settles to Saved', d.js("return document.getElementById('saveStatus').textContent") == 'Saved')

        # 2. failed save then Render: dependent action must stop
        d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK); d.js("window.__mode = 'fail'")
        type_into(d, '#pNotes', 'x'); time.sleep(0.2)
        d.js("document.querySelector('[data-k=bDraft]').click()")
        time.sleep(2.5)
        posted = d.js("return window.__log.filter(x => /\\/render$/.test(x.url)).length")
        check('2 Render not sent after failed save', posted == 0, posted)
        status = d.js("const s = document.getElementById('saveStatus'); return [s.textContent, s.classList.contains('err')]")
        check('2 error is shown', status[1] is True and status[0] != '', status)

        # 3. project switch while saving: late response must not touch the other project
        d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK); d.js('window.__delay = 2000')
        bp = server_project(beta); before_beta = bp['title'] or bp['name']
        type_into(d, '#pTitle', ' edited'); time.sleep(1.0)  # save in flight (delayed)
        d.js("document.activeElement.blur()")               # a real switch moves focus to the picker
        d.js("const p = document.getElementById('picker'); p.value = %s; p.dispatchEvent(new Event('change'))" % json.dumps(beta))
        time.sleep(3.5)
        shown = d.js("return document.getElementById('pTitle').value")
        check('3 other project shows its own title', shown == before_beta, shown)
        check('3 other project unchanged on disk', server_project(beta)['title'] in ('', before_beta))
        check('3 edit landed on the original project', server_project(alpha)['title'].endswith('edited'), server_project(alpha)['title'])

        # 3b. edit typed just before switching (still inside the 700 ms debounce) must not be dropped
        d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK)
        type_into(d, '#pNotes', 'late'); d.js("document.activeElement.blur()")
        d.js("const p = document.getElementById('picker'); p.value = %s; p.dispatchEvent(new Event('change'))" % json.dumps(beta))
        time.sleep(2.0)
        check('3b unsaved edit survives an immediate project switch', server_project(alpha).get('notes') == 'late', server_project(alpha).get('notes'))

        # 4. two tabs: stale tab must conflict, keep its draft, and not overwrite
        d.go(url(beta)); d.wait("document.getElementById('pTitle')"); tab1 = d.s('GET', '/window')
        tab2 = d.new_tab(); d.go(url(beta)); d.wait("document.getElementById('pTitle')")
        d.switch(tab1); type_into(d, '#pTitle', ' one'); time.sleep(2.0)
        t1 = server_project(beta)['title']
        d.switch(tab2); type_into(d, '#pTitle', ' two'); time.sleep(2.0)
        text = d.alert_text()
        check('4 conflict dialog appears in stale tab', bool(text), text)
        check('4 server keeps first tab edit while dialog open', server_project(beta)['title'] == t1, server_project(beta)['title'])
        if text:
            d.s('POST', '/alert/dismiss'); time.sleep(1.0)    # dismiss = confirm() false
        draft = d.js("return document.getElementById('pTitle').value")
        check('4 stale tab keeps its draft', draft.endswith(' two'), draft)
        d.s('DELETE', '/window') if False else None

        # 5. polling must not overwrite a dirty field
        d.switch(tab1); d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK); d.js("window.__mode = 'fail'")
        type_into(d, '#pNotes', 'draft-text'); time.sleep(0.3)
        p = server_project(alpha); p['notes'] = 'from-elsewhere'; projects.save(p)
        time.sleep(4.5)                                      # > 1 poll interval (3 s)
        notes = d.js("return document.getElementById('pNotes').value")
        check('5 poll leaves the dirty field alone', notes.endswith('draft-text') and 'from-elsewhere' not in notes, notes)
    finally:
        if d: d.close()
        web.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    failed = results.count(False)
    print(f'{len(results) - failed}/{len(results)} browser checks passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
