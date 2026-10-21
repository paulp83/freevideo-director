"""Browser acceptance of the Project settings -> Finishing switches, end to end:

  * a stub ComfyUI (tests/stub_comfy.py) with BOTH finishing node families dropped
    (COMFY_STUB_DROP: SeedVR2 classes and RIFE): while the project is legacy the Finishing
    chip must read Down;
  * a real finished take (fixture scene 3) whose enhance job record says CRF 24 while the
    project runs CRF 16 -- the warning line inside the settings dialog must name it;

then the UI drives the whole chain: changing the selects autosaves `finishing` into
project.json on disk (waited for, not assumed), the summary badge re-words itself, after a
reload the controls come back from disk, the per-project readiness (GET /api/readiness
?project=) turns Ready for a project that needs neither upscaler nor interpolator while the
global check stays Down, ArrowDown on a focused select is a real keyboard change that
autosaves, and Reset restores the legacy defaults -- chip Down again.

Needs `geckodriver` and `firefox` on PATH. Throwaway projects dir and ComfyUI root; never
touches real projects. Run: python3 tests/browser_finishing.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests' / 'fixtures'))
import finishing      # noqa: E402
import project as projects   # noqa: E402
import make_fixture   # noqa: E402

WEB_PORT, STUB_PORT, DRIVER_PORT = 8213, 8214, 4466
DROP = 'SeedVR2LoadDiTModel,SeedVR2LoadVAEModel,SeedVR2VideoUpscaler,RIFE VFI'
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
        ses = self.call('POST', '/session', caps)
        if 'sessionId' not in ses.get('value', {}):
            raise RuntimeError('geckodriver session failed: ' + json.dumps(ses)[:300])
        self.sid = ses['value']['sessionId']

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
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
        r = self.call(method, f'/session/{self.sid}{path}',
                      body if body is not None else ({} if method == 'POST' else None))
        if isinstance(r.get('value'), dict) and 'error' in r['value']:
            raise RuntimeError(r['value'].get('message', r['value']['error']))
        return r.get('value')

    def js(self, script, *args):
        return self.s('POST', '/execute/sync', {'script': script, 'args': list(args)})

    def go(self, url): self.s('POST', '/url', {'url': url})

    def wait(self, script, timeout=15, *args):
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

    def close(self):
        try: self.call('DELETE', f'/session/{self.sid}')
        finally: self.proc.terminate()

SET = """const set = (id, v) => { const e = document.getElementById(id);
  e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); };
set('finUpscale', arguments[0]); set('finInterp', arguments[1]);
set('finPreset', arguments[2]); return 1;"""

CONTROLS = """const g = id => document.getElementById(id).value;
  return {up: g('finUpscale'), fac: g('finInterp'), preset: g('finPreset'), crf: g('finCrf'),
          summary: document.getElementById('finSummary').textContent,
          warn: {hidden: document.getElementById('finWarn').hidden,
                 text: document.getElementById('finWarn').textContent}};"""


def disk(slug):
    return json.loads(projects.path(slug, 'project.json').read_text())


def wait_disk(slug, test, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            p = disk(slug)
        except Exception:
            time.sleep(0.2); continue
        if test(p.get('finishing')):
            return p
        time.sleep(0.2)
    return None


def api(path):
    with urllib.request.urlopen(f'http://127.0.0.1:{WEB_PORT}{path}', timeout=5) as r:
        return json.loads(r.read())


def main():
    tmp = Path(tempfile.mkdtemp(prefix='director-browser-finishing-'))
    man = make_fixture.build(tmp)            # real ffmpeg media; sets projects.ROOT
    slug, ids = man['slug'], man['scene_ids']

    # a finished take with a recorded enhance job: CRF 24 then, CRF 16 now -> flag 'changed'
    p = projects.load(slug)
    final_take = next(t for t in p['scenes'][2]['takes'] if t.get('final'))
    projects.write_job_snapshot(slug, {
        'id': 'fa11e7fa11e7fa11', 'kind': 'enhance', 'created': time.time(), 'project': slug,
        'scene': ids[2], 'source': {'take': final_take['id'], 'run': final_take['run'],
                                    'provenance': 'recorded'},
        'finishing': {'fps': 24, 'resolution': 1080, 'multiplier': 2, 'crf': 24,
                      'settings': finishing.clean_finishing({'encode': {'crf': 24}})}})
    projects.mutate(slug, lambda q: (projects.set_explicit(q, ids[2], final_take['id']),
                                     q['scenes'][2]['takes'] and
                                     next(t for t in q['scenes'][2]['takes']
                                          if t['id'] == final_take['id']).__setitem__(
                                              'final_job', 'fa11e7fa11e7fa11')))

    comfy_root = tmp / 'comfy-stub'
    comfy_root.mkdir()
    stub = subprocess.Popen([sys.executable, str(HERE / 'tests' / 'stub_comfy.py'),
                             '--port', str(STUB_PORT), '--root', str(comfy_root)],
                            env=dict(os.environ, COMFY_STUB_DROP=DROP),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    launcher = ('import sys; sys.path.insert(0, %r); import project; from pathlib import Path; '
                'project.ROOT = Path(%r); sys.argv = ["web.py", "--port", "%d"]; '
                'import runpy; runpy.run_path(%r, run_name="__main__")'
                % (str(HERE), str(projects.ROOT), WEB_PORT, str(HERE / 'web.py')))
    web = subprocess.Popen([sys.executable, '-c', launcher],
                           env=dict(os.environ,
                                    COMFY_URL=f'http://127.0.0.1:{STUB_PORT}',
                                    DIRECTOR_URL='http://127.0.0.1:1/v1/chat/completions',
                                    COMFY_ROOT=str(comfy_root),
                                    JOBS_DB=str(tmp / 'jobs.db')),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        for _ in range(50):
            try: api('/api/projects'); break
            except Exception: time.sleep(0.2)

        rep = api('/api/readiness')['checks']['finishing']
        check('global readiness is Down with both node families missing',
              rep['level'] == 'unavailable' and 'SeedVR2' in rep['reason'], rep['reason'])
        st = api(f'/api/p/{slug}')
        flag = st['scenes'][2]['status']['flags']['finishing_changed']
        check('the recorded CRF-24 final reads changed against today',
              flag['state'] == 'changed' and 'crf 24 -> 16' in flag['reason'],
              json.dumps(flag))

        d = Driver()
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6")
        check('legacy project shows Finishing Down with the upscaler missing',
              d.wait("document.getElementById('rd-finishing').classList.contains('down')"),
              d.js("return document.getElementById('rd-finishing').textContent") or '')

        d.js("document.getElementById('openSettings').click(); return 1;")
        d.wait("document.getElementById('settings').open")
        c = d.js(CONTROLS)
        check('controls start on the legacy defaults',
              (c['up'], c['fac'], c['preset'], c['crf']) == ('1080', '2', 'high', '16'),
              json.dumps(c))
        check('summary spells the legacy output (48 fps, CRF 16)',
              '48 fps' in c['summary'] and 'CRF 16' in c['summary'] and '1080' in c['summary'],
              c['summary'])
        check('the dialog warns about the final made with other settings',
              not c['warn']['hidden'] and '1 scene was enhanced' in c['warn']['text'],
              json.dumps(c['warn']))

        # interpolation off + quality standard: autosave must land on disk
        d.js(SET, '1080', '0', 'standard')
        saved = wait_disk(slug, lambda f: f and f['fps_policy'] == 'original'
                          and f['encode']['crf'] == 20)
        check('the autosave wrote finishing into project.json', saved is not None,
              json.dumps(saved or disk(slug).get('finishing')))
        if saved:
            f = saved['finishing']
            check('the saved object is canonical (derived preset, explicit policy)',
                  f['encode']['preset'] == 'standard' and not f['interpolation']['enabled']
                  and f['upscale'] == {'enabled': True, 'height': 1080}, json.dumps(f))
        c = d.js(CONTROLS)
        check('summary re-words itself: 24 fps now, CRF 20',
              '24 fps' in c['summary'] and 'CRF 20' in c['summary'], c['summary'])

        # upscaling off too -> this project needs nothing that is missing
        d.js(SET, '0', '0', 'standard')
        wait_disk(slug, lambda f: f and not f['upscale']['enabled'])
        per = api(f'/api/readiness?project={slug}')['checks']['finishing']
        glob = api('/api/readiness')['checks']['finishing']
        check('per-project readiness turns Ready while the global stays Down',
              per['level'] == 'ready' and glob['level'] == 'unavailable',
              json.dumps({'project': per['level'], 'global': glob['level'],
                          'reason': per.get('reason')}))
        check('the excused SeedVR2 rows stay visible as not needed',
              any(i['ok'] and 'not needed' in i['short'] for i in per['items']
                  if 'seedvr2' in i['name'].lower()), json.dumps([i['short'] for i in per['items']][:8]))

        # reload: the controls come back from disk, the chip reads the project's truth
        d.go(f'http://127.0.0.1:{WEB_PORT}/project?p={slug}')
        d.wait("document.querySelectorAll('#navigator .navitem').length === 6")
        check('the chip follows the project now: Finishing not Down',
              d.wait("document.getElementById('rd-finishing').textContent.includes('Ready')"),
              d.js("return document.getElementById('rd-finishing').textContent") or '')
        d.js("document.getElementById('openSettings').click(); return 1;")
        time.sleep(0.4)
        c = d.js(CONTROLS)
        check('after reload the controls show what is on disk',
              (c['up'], c['fac'], c['preset'], c['crf']) == ('0', '0', 'standard', '20'),
              json.dumps(c))
        d.wait("!document.getElementById('bEnhanceSel').title.includes('CRF-20')"
               " && !document.getElementById('bEnhanceSel').title.includes('unavailable')")

        btn = d.js("""const g = id => document.getElementById(id).title;
          return {enh: g('bEnhanceSel'), fin: g('bFinal'), exp: g('bExportFilm')};""")
        check('action help text names only the enabled stages',
              'SeedVR2' not in btn['enh'] and 'SeedVR2' not in btn['fin']
              and 'CRF-20' in btn['enh'] and 'CRF-20' in btn['fin']
              and '24 fps' in btn['exp'] and 'CRF-20' in btn['exp'], json.dumps(btn))
        # keyboard only: ArrowDown on the focused select changes and saves
        d.js("document.getElementById('finUpscale').focus(); return 1;")
        d.key('\ue015')                       # W3C keysym for ArrowDown
        saved = wait_disk(slug, lambda f: f and f['upscale']['enabled'] and f['upscale']['height'] == 720)
        check('ArrowDown on the focused select autosaves 720p', saved is not None,
              json.dumps(disk(slug).get('finishing', {}).get('upscale')))

        # Reset restores the pre-settings pipeline everywhere, chip included
        d.js("document.getElementById('finReset').click(); return 1;")
        saved = wait_disk(slug, lambda f: f == finishing.defaults())
        check('Reset wrote exactly the legacy defaults back', saved is not None,
              json.dumps(disk(slug).get('finishing')))
        c = d.js(CONTROLS)
        check('controls and summary snap back to 1080 / x2 / CRF 16 / 48 fps',
              (c['up'], c['fac'], c['preset'], c['crf']) == ('1080', '2', 'high', '16')
              and '48 fps' in c['summary'], json.dumps(c))
        check('the chip is Down again for the defaulted project',
              d.wait("document.getElementById('rd-finishing').classList.contains('down')"),
              d.js("return document.getElementById('rd-finishing').textContent") or '')
    except Exception as e:
        check('suite ran without exceptions', False, repr(e))
    finally:
        if d: d.close()
        for pr in (stub, web):
            pr.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(results)
    print(f'{passed}/{len(results)} browser-finishing checks passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
