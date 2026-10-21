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
    end = time.time() + timeout
    while time.time() < end:
        t = d.alert_text()
        if t:
            return t
        time.sleep(0.2)
    return None

def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-'))
    projects.ROOT = tmp / 'projects'
    alpha, beta = setup_projects()
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
        url = lambda slug: f'http://127.0.0.1:{WEB_PORT}/project/classic?p={slug}'

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

        # 4. two tabs: stale tab must conflict, refuse to overwrite, and reload the server copy on Cancel
        d.go(url(beta)); d.wait("document.getElementById('pTitle')"); tab1 = d.s('GET', '/window')
        tab2 = d.new_tab(); d.go(url(beta)); d.wait("document.getElementById('pTitle')"); d.js(HOOK)
        d.switch(tab1); type_into(d, '#pTitle', ' one'); time.sleep(2.0)
        t1 = server_project(beta)['title']
        d.switch(tab2); d.js("window.__freeze = true"); type_into(d, '#pTitle', ' two'); d.js("window.__freeze = false"); time.sleep(2.0)
        text = d.alert_text()
        check('4 conflict dialog appears in stale tab', bool(text), text)
        check('4 server keeps first tab edit while dialog open', server_project(beta)['title'] == t1, server_project(beta)['title'])
        if text:
            d.s('POST', '/alert/dismiss'); time.sleep(1.2)    # Cancel = Reload the server copy
        shown = d.js("return document.getElementById('pTitle').value")
        check('4 Cancel reloads the server copy in the stale tab', shown == t1, shown)
        d.s('DELETE', '/window') if False else None

        # 5. polling must not overwrite a dirty field
        d.switch(tab1); d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK); d.js("window.__mode = 'fail'")
        type_into(d, '#pNotes', 'draft-text'); time.sleep(0.3)
        p = server_project(alpha); p['notes'] = 'from-elsewhere'; projects.save(p)
        time.sleep(4.5)                                      # > 1 poll interval (3 s)
        notes = d.js("return document.getElementById('pNotes').value")
        check('5 poll leaves the dirty field alone', notes.endswith('draft-text') and 'from-elsewhere' not in notes, notes)

        # 6. take selection: choosing pins a take to disk; a new take does not move the pin; auto releases it
        p = server_project(alpha)
        sid = p['scenes'][0]['id']
        def take_rec(n, tid):
            run = str(tmp / 'takes' / ('take-%d' % n))
            return {'id': tid, 'run': run, 'path': run + '/video.mp4', 'url': '/files/x.mp4',
                    'preset': 'draft', 'seed': 100 + n, 'created': time.time(),
                    'final': None, 'final_url': None, 'variant': None}
        run1 = take_rec(1, 'aaaa0001')['run']
        p['scenes'][0]['takes'] = [take_rec(1, 'aaaa0001'), take_rec(2, 'bbbb0002')]
        projects.save(p)
        d.go(url(alpha))
        d.wait("document.querySelectorAll('#scene-" + sid + " .take [data-k=bChoose]').length === 2")
        badges = d.js("return [...document.querySelectorAll('#scene-" + sid + " .take .badge')].map(b => b.textContent)")
        check('6 latest take is the automatic choice', 'latest (auto)' in badges and '★ chosen' not in badges, badges)
        d.js("document.querySelectorAll('#scene-" + sid + " .take [data-k=bChoose]')[1].click()")  # pin the older take
        def scene_disk():
            return next(s for s in server_project(alpha)['scenes'] if s['id'] == sid)
        for _ in range(40):
            if scene_disk().get('selection') == 'explicit':
                break
            time.sleep(0.2)
        sp = scene_disk()
        check('6 choose click pins the take on disk',
              sp.get('selection') == 'explicit' and sp.get('selected_take') == 'aaaa0001' and sp.get('star') == run1, sp)
        labels = None
        for _ in range(40):                                   # the 3 s poll redraws the rows
            labels = d.js("return [...document.querySelectorAll('#scene-" + sid + " .take [data-k=bChoose]')].map(b => b.textContent)")
            if labels == ['☆ choose', '★ chosen']:
                break
            time.sleep(0.2)
        check('6 page marks the pinned take chosen', labels == ['☆ choose', '★ chosen'], labels)
        d.js("document.querySelector('#scene-" + sid + " [data-k=bAuto]').click()")
        for _ in range(40):
            if scene_disk().get('selection') == 'auto':
                break
            time.sleep(0.2)
        sp = scene_disk()
        check('6 auto releases the pin on disk',
              sp.get('selection') == 'auto' and sp.get('star') is None and sp.get('selected_take') is None, sp)
        d.js("document.querySelectorAll('#scene-" + sid + " .take [data-k=bChoose]')[1].click()")  # pin again
        for _ in range(40):
            if scene_disk().get('selection') == 'explicit':
                break
            time.sleep(0.2)
        p = server_project(alpha)                               # a new take finishes while pinned
        p['scenes'][0]['takes'].append(take_rec(3, 'cccc0003'))
        projects.save(p)
        time.sleep(4.5)                                         # > 1 poll interval (3 s)
        sp = scene_disk()
        check('6 new take does not move the pinned choice on disk',
              sp['selection'] == 'explicit' and sp['selected_take'] == 'aaaa0001', sp)
        labels = d.js("return [...document.querySelectorAll('#scene-" + sid + " .take [data-k=bChoose]')].map(b => b.textContent)")
        check('6 page still marks the pinned take chosen after a new take arrives',
              labels == ['☆ choose', '☆ choose', '★ chosen'], labels)

        # 7. a real 409 mid-edit: OK (Keep mine) resends the draft and keeps a take recorded server-side
        p = server_project(alpha)
        p['scenes'][0]['takes'].append(take_rec(9, 'dddd0009'))    # a render finished while the user typed
        projects.save(p)
        takes_now = len(p['scenes'][0]['takes'])
        d.go(url(alpha)); d.wait("document.getElementById('pTitle')"); d.js(HOOK)
        d.js("window.__freeze = true")          # no poll may adopt the new rev before the draft is dirty
        projects.save_editor(alpha, {'title': 'from-elsewhere'})   # another editor wins the title first
        type_into(d, '#pTitle', ' mine')
        d.js("window.__freeze = false")
        text = wait_alert(d)
        check('7 a 409 opens the conflict dialog naming the field', bool(text) and 'title' in text, text)
        d.s('POST', '/alert/accept')                               # OK = Keep mine
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
        d.s('POST', '/alert/dismiss')                              # Cancel = Reload
        time.sleep(1.2)
        shown = d.js("return document.getElementById('pTitle').value")
        check('7b Reload shows the server copy', shown == 'server-won', shown)
        type_into(d, '#pTitle', ' after-reload'); time.sleep(1.5)
        check('7b Reload adopted the server revision: the next save merges cleanly',
              server_project(alpha)['title'] == 'server-won after-reload', server_project(alpha)['title'])

        # 8. the 3 s poll landing while a save is in flight must not clobber the draft
        d.go(url(alpha)); d.wait("document.getElementById('pNotes')"); d.js(HOOK); d.js('window.__delay = 4000')
        type_into(d, '#pNotes', ' mid-flight')
        time.sleep(3.4)                                            # the poll has redrawn; the save is still pending
        notes = d.js("return document.getElementById('pNotes').value")
        check('8 poll during an in-flight save leaves the draft in the field', notes.endswith(' mid-flight'), notes)
        d.js('window.__delay = 0')
        landed = d.wait("document.getElementById('saveStatus').textContent === 'Saved'", 10)
        check('8 the in-flight save lands after the poll', bool(landed) and server_project(alpha)['notes'].endswith(' mid-flight'), server_project(alpha)['notes'])

        # 9. Assemble with edits that cannot save: flushEdits rejects, the click posts nothing, and nothing hammers /save
        p = server_project(alpha)
        p['scenes'][1]['takes'].append(take_rec(8, 'eeee0008'))    # every scene has a take: the button is enabled
        projects.save(p)
        clickable = d.wait("[...document.querySelectorAll('#steps button')].some(b => b.textContent === 'Assemble' && !b.disabled)", 10)
        check('9 the Assemble button became clickable', bool(clickable))
        d.js("window.__mode = 'fail'")
        type_into(d, '#pTitle', ' doomed'); time.sleep(1.2)        # one doomed save attempt
        clicked = d.js("const b = [...document.querySelectorAll('#steps button')].find(x => x.textContent === 'Assemble' && !x.disabled); if (b) b.click(); return !!b")
        check('9 clicked Assemble', clicked is True)
        time.sleep(1.5)
        posted = d.js("return window.__log.filter(x => /\\/assemble/.test(x.url)).length")
        check('9 Assemble never reached the server while edits were unsaved', posted == 0, posted)
        status = d.js("const s = document.getElementById('saveStatus'); return [s.textContent, s.classList.contains('err')]")
        check('9 the flushEdits rejection is surfaced in the status line',
              status[1] is True and 'draft is still here' in status[0], status)
        saves_a = d.js("return window.__log.filter(x => x.url.endsWith('/save')).length")
        time.sleep(1.5)
        saves_b = d.js("return window.__log.filter(x => x.url.endsWith('/save')).length")
        check('9 the failed save is not retried in a loop', saves_b - saves_a <= 1, saves_b - saves_a)

        # 10. cut fingerprint: a stored cut whose music differs shows as outdated with the component
        # named; a cut from before fingerprints existed shows as legacy
        p = server_project(alpha)
        plan = projects.cut_plan(p, projects.current_used_takes(p, 'animatic'), 'animatic')
        plan['music'] = {'file': 'music.mp3', 'hash': 'old-song', 'brief_hash': 'old-brief',
                         'volume': '0.6', 'loudnorm': 'I=-14:TP=-1.5:LRA=11'}   # the bed under the cut differs
        p['outputs'] = {'animatic': {'file': 'animatic-old.mp4', 'seconds': 18.0, 'scenes': 2, 'not_final': [],
                                     'used_takes': [], 'created': time.time(), 'fingerprint': 'deadbeef', 'cut_plan': plan},
                        'film': {'file': 'film-old.mp4', 'seconds': 18.0, 'scenes': 2, 'not_final': [],
                                 'used_takes': [], 'created': time.time()}}      # no fingerprint: legacy
        projects.save(p)
        d.go(url(alpha))
        landed = d.wait("document.getElementById('animaticInfo').textContent.includes('outdated')", 10)
        anim = d.js("return document.getElementById('animaticInfo').textContent")
        check('10 the outdated cut names the component that differs', bool(landed) and 'music' in anim, anim)
        film = d.js("return document.getElementById('filmInfo').textContent")
        check('10 a legacy cut reports it predates cut fingerprints', 'predates cut fingerprints' in film, film)
    finally:
        if d: d.close()
        web.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    failed = results.count(False)
    print(f'{len(results) - failed}/{len(results)} browser checks passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
