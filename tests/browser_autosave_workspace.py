"""Browser check of the WORKSPACE page's autosave (project.html; tests/browser_autosave.py covers /project/classic): headless Firefox through geckodriver's raw WebDriver HTTP API.

Needs `geckodriver` and `firefox` on PATH. Starts its own web.py on a spare port against a throwaway
projects dir; never touches real projects. Run: python3 tests/browser_autosave.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import leak_guard     # noqa: E402
import project as projects   # noqa: E402

WEB_PORT, DRIVER_PORT = 8199, 4455
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

    def key(self, ch):
        self.s('POST', '/actions', {'actions': [{'type': 'key', 'id': 'k',
                                                 'actions': [{'type': 'keyDown', 'value': ch},
                                                             {'type': 'keyUp', 'value': ch}]}]})

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: leak_guard.stop(self.proc)


HOOK = """
window.__log = []; window.__mode = 'pass'; window.__delay = 0; window.__freeze = false;
const real = window.fetch.bind(window);
window.fetch = async (url, opts) => {
  const u = String(url); const isSave = u.endsWith('/save');
  window.__log.push({url: u, body: opts && opts.body});
  if (window.__freeze && !isSave && u.includes('/api/p/')) return new Promise(function () {});
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



def wait_alert(d, timeout=8):
    """The conflict dialog is an in-page <dialog> now (a native confirm's Cancel used to discard the draft): return its
    text once it is open, or None."""
    return d.wait("(document.getElementById('conflictAsk').open && document.getElementById('conflictWhat').textContent) || ''", timeout)


def conflict_state(d):
    return d.js("""return {open: document.getElementById('conflictAsk').open,
                            bar: !document.getElementById('conflictBar').hidden,
                            focus: document.activeElement && document.activeElement.id,
                            title: document.getElementById('pTitle').value,
                            status: document.getElementById('saveStatus').textContent};""")


def load(d, slug):
    """Open a film in the workspace and open Project settings, where the title, bible and notes fields live."""
    d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
    d.wait("document.getElementById('openSettings') && !document.getElementById('openSettings').hidden", 20)
    d.js("document.getElementById('openSettings').click(); return 1;")
    d.wait("document.getElementById('settings').open", 10)


def click(d, css):
    d.js("document.querySelector(arguments[0]).click(); return 1;", css)

def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-'))
    projects.ROOT = tmp / 'projects'
    alpha, beta = setup_projects()
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; project.ROOT = Path(%r); '
                'sys.argv = ["web.py", "--port", "%d"]; import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    web = leak_guard.spawn([sys.executable, '-c', launcher],
                           env=dict(os.environ, JOBS_DB=str(tmp / 'jobs.db')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}/api/projects', timeout=1); break
            except Exception: time.sleep(0.2)
        d = Driver()
        url = lambda slug: f'http://127.0.0.1:{WEB_PORT}/project?p={slug}'

        # 1. typing while a save is delayed: the edit made during the request must not be lost
        load(d, alpha)
        d.js(HOOK); d.js('window.__delay = 1500')
        type_into(d, '#pBible', 'A'); time.sleep(1.1)       # save fired (700 ms debounce), response still pending
        type_into(d, '#pBible', 'B')
        d.js('window.__delay = 0')
        saved = d.wait("document.getElementById('saveStatus').textContent === 'Saved' && !window.__log.some(x => 0)", 10)
        time.sleep(2.5)                                     # let the follow-up save land
        check('1 edit typed during delayed save survives', server_project(alpha).get('bible') == 'AB', server_project(alpha).get('bible'))
        check('1 status settles to Saved', d.js("return document.getElementById('saveStatus').textContent") == 'Saved')

        # 2. failed save then a dependent action: run() is the one door every action button goes through (it awaits
        #    flushEdits); calling it directly cannot be defeated by a button that happens to be disabled in this fixture
        load(d, alpha); d.js(HOOK); d.js("window.__mode = 'fail'")
        type_into(d, '#pNotes', 'x'); time.sleep(0.2)
        d.js("document.getElementById('settings').close(); run('assemble', {kind: 'animatic'}); return 1;")
        time.sleep(2.5)
        posted = d.js("return window.__log.filter(x => /\\/(assemble|render|prompt|enhance)$/.test(x.url)).length")
        check('2 the action is not sent after a failed save', posted == 0, posted)
        status = d.js("const s = document.getElementById('saveStatus'); return [s.textContent, s.classList.contains('err')]")
        check('2 the error says nothing was saved and the draft is kept', status[1] is True and 'not saved' in status[0].lower() and 'draft' in status[0].lower(), status)

        # 3. project switch while saving: late response must not touch the other project
        load(d, alpha); d.js(HOOK); d.js('window.__delay = 2000')
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
        load(d, alpha); d.js(HOOK)
        type_into(d, '#pNotes', 'late'); d.js("document.activeElement.blur()")
        d.js("const p = document.getElementById('picker'); p.value = %s; p.dispatchEvent(new Event('change'))" % json.dumps(beta))
        time.sleep(2.0)
        check('3b unsaved edit survives an immediate project switch', server_project(alpha).get('notes') == 'late', server_project(alpha).get('notes'))

        # 4. two tabs: the stale tab must conflict, refuse to overwrite, and KEEP its draft until the user chooses
        load(d, beta); tab1 = d.s('GET', '/window')
        tab2 = d.new_tab(); load(d, beta); d.js(HOOK)
        d.switch(tab1); type_into(d, '#pTitle', ' one'); time.sleep(2.0)
        t1 = server_project(beta)['title']
        d.switch(tab2); d.js("window.__freeze = true"); type_into(d, '#pTitle', ' two'); d.js("window.__freeze = false"); time.sleep(2.0)
        text = wait_alert(d)
        check('4 conflict dialog appears in stale tab and names the field', bool(text) and 'title' in text, text)
        check('4 server keeps first tab edit while dialog open', server_project(beta)['title'] == t1, server_project(beta)['title'])
        check('4 the safe choice has focus (Decide later), not Keep mine', conflict_state(d)['focus'] == 'conflictLater', conflict_state(d))
        d.key('\ue00c')                                              # Escape = decide later, nothing is lost
        time.sleep(0.8)
        cs = conflict_state(d)
        check('4 Escape keeps the draft in the field', cs['title'].endswith(' two'), cs)
        check('4 Escape leaves the conflict visible as a banner, dialog closed', (not cs['open']) and cs['bar'], cs)
        check('4 the status says the draft is kept, not saved', 'draft is kept' in cs['status'], cs)
        time.sleep(3.5)                                               # a poll must not adopt the server copy over the draft
        cs = conflict_state(d)
        check('4 a poll does not discard the pending draft', cs['title'].endswith(' two') and cs['bar'], cs)
        check('4 nothing was written while the conflict is open', server_project(beta)['title'] == t1, server_project(beta)['title'])
        click(d, '#conflictBarReload')
        time.sleep(1.2)
        shown = d.js("return document.getElementById('pTitle').value")
        check('4 Reload (from the banner) shows the server copy and clears the banner',
              shown == t1 and conflict_state(d)['bar'] is False, shown)
        d.s('DELETE', '/window') if False else None

        # 5. polling must not overwrite a dirty field
        d.switch(tab1); load(d, alpha); d.js(HOOK); d.js("window.__mode = 'fail'")
        type_into(d, '#pNotes', 'draft-text'); time.sleep(0.3)
        p = server_project(alpha); p['notes'] = 'from-elsewhere'; projects.save(p)
        time.sleep(4.5)                                      # > 1 poll interval (3 s)
        notes = d.js("return document.getElementById('pNotes').value")
        check('5 poll leaves the dirty field alone', notes.endswith('draft-text') and 'from-elsewhere' not in notes, notes)

        sid = server_project(alpha)['scenes'][0]['id']
        def take_rec(n, tid):
            run = str(tmp / 'takes' / ('take-%d' % n))
            return {'id': tid, 'run': run, 'path': run + '/video.mp4', 'url': '/files/x.mp4',
                    'preset': 'draft', 'seed': 100 + n, 'created': time.time(),
                    'final': None, 'final_url': None, 'variant': None}

        # 7. a real 409 mid-edit: OK (Keep mine) resends the draft and keeps a take recorded server-side
        p = server_project(alpha)
        p['scenes'][0]['takes'].append(take_rec(9, 'dddd0009'))    # a render finished while the user typed
        projects.save(p)
        takes_now = len(p['scenes'][0]['takes'])
        load(d, alpha); d.js(HOOK)
        d.js("window.__freeze = true")          # no poll may adopt the new rev before the draft is dirty
        projects.save_editor(alpha, {'title': 'from-elsewhere'})   # another editor wins the title first
        type_into(d, '#pTitle', ' mine')
        d.js("window.__freeze = false")
        text = wait_alert(d)
        check('7 a 409 opens the conflict dialog naming the field', bool(text) and 'title' in text, text)
        click(d, '#conflictMine')                                  # Keep mine
        d.wait("document.getElementById('saveStatus').textContent === 'Saved'", 10)
        time.sleep(0.5)
        sp = next(s for s in server_project(alpha)['scenes'] if s['id'] == sid)
        check('7 Keep mine lands the draft', server_project(alpha)['title'].endswith(' mine'), server_project(alpha)['title'])
        check('7 Keep mine does not drop the take recorded server-side',
              len(sp['takes']) == takes_now and sp['takes'][-1]['id'] == 'dddd0009', sp['takes'])

        # 7b. Cancel (Reload) discards the draft and adopts the server revision
        d.js("window.__freeze = true")
        projects.save_editor(alpha, {'title': 'server-won'})
        type_into(d, '#pTitle', ' mine2')
        d.js("window.__freeze = false")
        text = wait_alert(d)
        check('7b a second conflict opens the dialog again', bool(text), text)
        click(d, '#conflictReload')                                # Reload
        time.sleep(1.2)
        shown = d.js("return document.getElementById('pTitle').value")
        check('7b Reload shows the server copy', shown == 'server-won', shown)
        type_into(d, '#pTitle', ' after-reload'); time.sleep(1.5)
        check('7b Reload adopted the server revision: the next save merges cleanly',
              server_project(alpha)['title'] == 'server-won after-reload', server_project(alpha)['title'])

        # 7c. Decide later, then Keep mine from the banner: the draft lands on top of the newer copy
        d.js("window.__freeze = true")
        projects.save_editor(alpha, {'title': 'server-won-again'})
        type_into(d, '#pTitle', ' later')
        d.js("window.__freeze = false")
        text = wait_alert(d)
        check('7c a third conflict opens the dialog', bool(text), text)
        click(d, '#conflictLater')                                 # Decide later (button, not Escape)
        time.sleep(0.6)
        cs = conflict_state(d)
        check('7c Decide later keeps the draft and the banner', cs['title'].endswith(' later') and cs['bar'] and not cs['open'], cs)
        check('7c the server copy is untouched meanwhile', server_project(alpha)['title'] == 'server-won-again', server_project(alpha)['title'])
        click(d, '#conflictBarMine')
        d.wait("document.getElementById('saveStatus').textContent === 'Saved'", 10)
        time.sleep(0.5)
        check('7c Keep mine from the banner lands the draft (its whole text wins over the newer copy)',
              server_project(alpha)['title'] == 'server-won after-reload later', server_project(alpha)['title'])
        check('7c the banner is gone once resolved', conflict_state(d)['bar'] is False)

        # 8. the 3 s poll landing while a save is in flight must not clobber the draft
        load(d, alpha); d.js(HOOK); d.js('window.__delay = 4000')
        type_into(d, '#pNotes', ' mid-flight')
        time.sleep(3.4)                                            # the poll has redrawn; the save is still pending
        notes = d.js("return document.getElementById('pNotes').value")
        check('8 poll during an in-flight save leaves the draft in the field', notes.endswith(' mid-flight'), notes)
        d.js('window.__delay = 0')
        landed = d.wait("document.getElementById('saveStatus').textContent === 'Saved'", 10)
        check('8 the in-flight save lands after the poll', bool(landed) and server_project(alpha)['notes'].endswith(' mid-flight'), server_project(alpha)['notes'])


    finally:
        if d: d.close()
        leak_guard.stop(web)
        shutil.rmtree(tmp, ignore_errors=True)
    failed = results.count(False)
    for pid, cmd in leak_guard.final_leaks():
        print(f'LEAK - leftover process {pid}: {cmd[:100]}')
        failed += 1
    print(f'{len(results) - results.count(False)}/{len(results)} browser checks passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
