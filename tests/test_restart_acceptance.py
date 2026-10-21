#!/usr/bin/env python3
"""Restart acceptance: SIGKILL the director at every crash point and restart it on the same
temp root and jobs.db, against a stub ComfyUI that lives in its OWN process (tests/stub_comfy.py).

Every stage is one acceptance scenario from the design:

  S1  killed during preparation, before any submit          -> unknown 'nothing was submitted', retry
  S2  killed inside the submit, after the id was recorded    -> unknown 'submit was not confirmed',
                                                                scene stays reserved, Retry replays
  S3  killed inside the acceptance window (prompt in queue)  -> re-attach, no new submit
  S4  killed while running                                   -> re-attach, no new submit
  S5  prompt completes while the director is dead            -> completion recorded once on restart
  S6  killed in the result-recording window (raced)          -> take exactly once after restart
  S7  enhance chain killed mid-stage, stage later fails      -> failed with the backend error,
                                                                temp dir cleaned, scene free
  S8  render-final killed before finishing stages exist      -> main result recorded, finishing NOT
                                                                auto-submitted, honest message
  S9  backend unreachable at restart, then a fresh backend   -> 'not reachable yet', row untouched,
                                                                then 'no record' once; a second
                                                                restart reaches the same verdict
  S10 finished job restarts twice                            -> recovery is a no-op

Global invariants per stage: the stub counts the /prompt bodies it ever saw (no stage but the
explicit Retry may add one), project.json stays parseable with unique take ids, and jobs.db
never keeps a non-terminal 'submitting' row that recovery did not mark as recovering.

Slow by construction (real processes, real kills): skipped unless DIRECTOR_SLOW_SUITES=1.
"""
import json, os, shutil, signal, socket, sqlite3, subprocess, sys, tempfile, time
import unittest
import urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import project as projects                                 # noqa: E402

PLAN = {'title': 'Restart film', 'bible': 'nothing in here', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'a cat walks left'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'a cat sits'}]}

# a real 1x1 PNG: a project WITH refs uploads its reference on submit, so the upload hold
# makes the preparation window (S1) deterministic
PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63'
    '0001010000f8ff3f00054313a5f60000000049454e44ae426082')

RECOVER_STATES = ('preparing', 'submitting', 'queued', 'submitted', 'running')


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    """One temp root, one stub subprocess, one real `python3 web.py` subprocess (restartable
    at the same port against the same jobs.db)."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-restart-'))
        self.projects_root = self.root / 'projects'
        self.comfy_root = self.root / 'comfy'
        # readiness checks the ComfyUI folders, so the fake install has them from the start
        (self.comfy_root / 'input').mkdir(parents=True, exist_ok=True)
        (self.comfy_root / 'output').mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'jobs.db'
        self.stub_port = free_port()
        self.web_port = free_port()
        self.stub = self.web = None
        self.stub_log = open(self.root / 'stub.log', 'wb')
        self.web_log = open(self.root / 'web.log', 'wb')
        self.start_stub()

    # -- processes -----------------------------------------------------------------
    def env(self):
        return dict(os.environ, PROJECTS_ROOT=str(self.projects_root), JOBS_DB=str(self.db),
                    STATS=str(self.root / 'stats.json'), COMFY_ROOT=str(self.comfy_root),
                    COMFY_URL=f'http://127.0.0.1:{self.stub_port}',
                    POLL_SECONDS='0.15', RECONCILE_SECONDS='1.2', JOB_TIMEOUT='120')

    def start_stub(self, port=None):
        self.stub_port = port or self.stub_port
        self.stub = subprocess.Popen([sys.executable, str(HERE / 'stub_comfy.py'),
                                      '--port', str(self.stub_port), '--root', str(self.comfy_root)],
                                     stdout=self.stub_log, stderr=subprocess.STDOUT)
        self.wait_http(f'http://127.0.0.1:{self.stub_port}/_ctl/state', 10)

    def start_web(self):
        self.web = subprocess.Popen([sys.executable, str(REPO / 'web.py'), '--port', str(self.web_port)],
                                    cwd=str(REPO), env=self.env(),
                                    stdout=self.web_log, stderr=subprocess.STDOUT)
        self.wait_http(f'http://127.0.0.1:{self.web_port}/api/queue', 20)

    def restart_web(self):
        assert self.web is None, 'kill the director before restarting it'
        self.start_web()

    def kill_web(self):
        os.kill(self.web.pid, signal.SIGKILL)
        self.web.wait()
        self.web = None

    def kill_stub(self):
        os.kill(self.stub.pid, signal.SIGKILL)
        self.stub.wait()
        self.stub = None

    def shutdown(self):
        for p in (self.web, self.stub):
            if p and p.poll() is None:
                p.kill()
                p.wait()
        for f in (self.stub_log, self.web_log):
            f.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def tail(self, name):
        return open(self.root / name, 'rb').read().decode(errors='replace')

    # -- http ----------------------------------------------------------------------
    def wait_http(self, url, secs):
        deadline = time.time() + secs
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2):
                    return
            except (urllib.error.HTTPError, OSError):
                time.sleep(0.1)
        raise AssertionError(f'{url} never came up within {secs}s')

    def wait_until(self, what, fn, secs=15):
        deadline = time.time() + secs
        value = None
        while time.time() < deadline:
            try:
                value = fn()
            except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
                value = None
            if value:
                return value
            time.sleep(0.15)
        raise AssertionError(f'{what}: last value {value!r} (after {secs}s)')

    def api(self, method, path, payload=None, base='web'):
        port = self.web_port if base == 'web' else self.stub_port
        data = json.dumps(payload if payload is not None else {}).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode() or 'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode(errors='replace') or 'null')

    # -- stub control ---------------------------------------------------------------
    def ctl(self, cmd, **body):
        return self.api('POST', f'/_ctl/{cmd}', body, base='stub')[1]

    def stub_state(self):
        return self.api('GET', '/_ctl/state', base='stub')[1]

    def stub_prompt_count(self):
        return len(self.stub_state()['prompt_bodies'])

    # -- durable state --------------------------------------------------------------
    def db_rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute('SELECT * FROM jobs')]
        finally:
            conn.close()
        for r in rows:
            r['holds'] = json.loads(r['holds'] or '[]')
            r['prompt_ids'] = json.loads(r['prompt_ids'] or '[]')
        return rows

    def db_row(self, job_id):
        return next((r for r in self.db_rows() if r['id'] == job_id), None)

    def project_json(self, slug):
        return json.loads((self.projects_root / slug / 'project.json').read_text())

    # -- the director's view --------------------------------------------------------
    def queue_jobs(self):
        return self.api('GET', '/api/queue')[1]['jobs']

    def queue_job(self, job_id):
        return next((j for j in self.queue_jobs() if j['id'] == job_id), None)

    def scene_job(self, view, scene_id):
        return next((s.get('job') for s in view['scenes'] if s['id'] == scene_id), None)


@unittest.skipUnless(os.environ.get('DIRECTOR_SLOW_SUITES'),
                     'slow: real OS processes and SIGKILLs; set DIRECTOR_SLOW_SUITES=1')
class RestartAcceptance(unittest.TestCase):
    """Stages S1-S10; one harness (temp root + stub + real web.py subprocess) per stage.
    Nothing here ever touches the repo's projects/, stats.json or jobs.db."""

    def setUp(self):
        self.h = Harness()
        self.h.start_web()

    def tearDown(self):
        if hasattr(self, 'h'):
            self.h.shutdown()

    # -- fixture --------------------------------------------------------------------
    def make_project(self, with_refs=False):
        projects.ROOT = self.h.projects_root
        refs = [('cat.png', PNG)] if with_refs else []
        slug = projects.create('Restart film', 'nothing in here', 30, '16:9', refs)['slug']
        projects.mutate(slug, lambda p: projects.apply_storyboard(p, PLAN))
        def give_prompts(p):
            for s in p['scenes']:
                s['prompt'] = 'a cat walks left'
        projects.mutate(slug, give_prompts)
        self.slug = slug
        self.s1, self.s2 = [s['id'] for s in projects.load(slug)['scenes']]
        return slug

    def new_job_id(self, kind):
        """The id of the newest job of `kind` for this project, straight out of jobs.db --
        it is durable the moment the endpoint accepts the job, even if the job is already gone."""
        def find():
            ids = [r['id'] for r in self.h.db_rows()
                   if r['project'] == self.slug and r['kind'] == kind]
            return ids[-1] if ids else None
        return self.h.wait_until(f'jobs.db holds a {kind} row', find, secs=10)

    def render(self, scene=None, preset='draft'):
        code, body = self.h.api('POST', f'/api/p/{self.slug}/render',
                                {'scene': scene or self.s1, 'preset': preset})
        self.assertEqual(code, 200, body)
        return self.new_job_id('render-final' if preset == 'final' else 'render-draft')

    def takes(self, scene_index=0):
        return self.h.project_json(self.slug)['scenes'][scene_index].get('takes') or []

    def assert_invariants(self, expects_recovering=True):
        for r in self.h.db_rows():
            if r['state'] == 'submitting' and not r['resolved_verdict']:
                self.assertIsNotNone(r['recovering_at'],
                                     f"unexplained non-terminal 'submitting' row {r['id']}")
        if expects_recovering:
            pend = [r for r in self.h.db_rows() if r['state'] in RECOVER_STATES]
            self.assertTrue(all(r['recovering_at'] is not None for r in pend),
                            [r['id'] for r in pend if r['recovering_at'] is None])
        ids = [t['id'] for sc in self.h.project_json(self.slug)['scenes'] for t in sc.get('takes') or []]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate take ids')

    # -- S1: preparation crash, nothing submitted -----------------------------------
    def test_s1_crash_before_any_submit(self):
        self.make_project(with_refs=True)
        self.h.ctl('hold-uploads')                                # prep stalls on the ref upload
        job_id = self.render()
        self.h.wait_until('the upload hold caught preparation', lambda: self.h.stub_state()['uploads'] > 0)
        self.assertEqual(self.h.stub_prompt_count(), 0, 'the submit started before we could kill')
        self.h.kill_web()
        row = self.h.db_row(job_id)
        self.assertIn(row['state'], RECOVER_STATES)
        self.assertEqual(row['prompt_ids'], [])
        self.h.restart_web()
        job = self.h.wait_until('recovery verdict', lambda: self.h.queue_job(job_id))
        self.assertEqual(job['state'], 'unknown')
        self.assertTrue(job['error'].startswith('the job was interrupted before anything reached ComfyUI'),
                        job['error'])
        view = self.h.api('GET', f'/api/p/{self.slug}')[1]
        scene_job = self.h.scene_job(view, self.s1)
        self.assertEqual(scene_job['state'], 'unknown')           # honest state text in the project view
        self.assertTrue(scene_job['retry_allowed'])
        code, _ = self.h.api('POST', f'/api/p/{self.slug}/prompt', {'scene': self.s1})
        self.assertNotEqual(code, 409, 'the scene stayed blocked after an unrecoverable never-submitted job')
        self.assert_invariants(expects_recovering=False)          # the row is terminal now
        self.assertEqual(self.h.stub_prompt_count(), 0)           # recovery never submitted

    # -- S2: crash inside the submit; Retry replays recorded inputs ------------------
    def test_s2_crash_inside_submit_then_retry(self):
        self.make_project()
        self.h.ctl('mode', mode='hold')                           # /prompt recorded, never answered
        job_id = self.render()
        self.h.wait_until('the submit reached the stub', lambda: self.h.stub_state()['prompt_bodies'])
        self.h.kill_web()
        row = self.h.db_row(job_id)
        self.assertEqual(row['state'], 'submitting')
        self.assertEqual(len(row['prompt_ids']), 1, 'the id must be durable BEFORE the POST left')
        self.h.restart_web()
        job = self.h.wait_until('recovery verdict', lambda: self.h.queue_job(job_id))
        self.assertEqual(job['state'], 'unknown')
        self.assertTrue(job['error'].startswith('the submit was not confirmed'), job['error'])
        self.assertTrue(job['retry_allowed'])
        self.assertEqual(self.h.stub_prompt_count(), 1)           # no resubmission
        code, _ = self.h.api('POST', f'/api/p/{self.slug}/prompt', {'scene': self.s1})
        self.assertEqual(code, 409, 'an unconfirmed submit must keep the scene reserved')
        self.h.ctl('mode', mode='auto')
        code, retry = self.h.api('POST', f'/api/job/{job_id}/retry', {})
        self.assertEqual(code, 200, retry)
        new_id = retry['job']['id']
        self.assertEqual(retry['retry_of'], job_id)               # honestly labelled as a replay
        self.assertNotEqual(new_id, job_id)
        self.h.wait_until('the retry took', lambda: len(self.takes()) == 1)
        self.assertEqual(self.h.stub_prompt_count(), 2)           # exactly the Retry added one
        a, b = self.h.stub_state()['prompt_bodies']
        self.assertEqual(a['prompt'], b['prompt'], 'Retry must replay the recorded graph, not rebuild it')
        self.assertEqual(self.h.queue_job(new_id)['state'], 'finished')
        self.assertEqual(len(self.takes()), 1)

    # -- S3: crash inside the acceptance window ---------------------------------------
    def test_s3_crash_in_acceptance_window_reattaches(self):
        self.make_project()
        self.h.ctl('mode', mode='run-noresp')                     # accepted, response never lands
        job_id = self.render()
        st = self.h.wait_until('the prompt queued while the response hung',
                               lambda: (lambda s: s if s['running'] else None)(self.h.stub_state()))
        pid = st['running'][0]
        self.h.kill_web()
        row = self.h.db_row(job_id)
        self.assertEqual(row['state'], 'submitting')
        self.assertIn(pid, row['prompt_ids'])
        self.h.restart_web()
        self.h.wait_until('prompt still queued (no resubmit)', lambda: self.h.stub_state()['running'])
        self.assertEqual(self.h.stub_prompt_count(), 1)           # re-attach, no new submit
        self.h.ctl('complete', prompt_id=pid)
        self.h.wait_until('take recorded after re-attach', lambda: len(self.takes()) == 1)
        self.assertEqual(self.h.queue_job(job_id)['state'], 'finished')
        self.assert_invariants(expects_recovering=False)

    # -- S4: crash while running -------------------------------------------------------
    def test_s4_crash_while_running_reattaches(self):
        self.make_project()
        self.h.ctl('mode', mode='run')
        job_id = self.render()
        self.h.wait_until('job running', lambda: (self.h.queue_job(job_id) or {}).get('state') == 'running')
        pid = self.h.stub_state()['running'][0]
        self.h.kill_web()
        self.assertEqual(self.h.db_row(job_id)['state'], 'running')
        self.h.restart_web()
        self.h.wait_until('re-attached to the running prompt', lambda: self.h.stub_state()['running'])
        self.assertEqual(self.h.stub_prompt_count(), 1)           # re-attach, no new submit
        self.h.ctl('complete', prompt_id=pid)
        self.h.wait_until('take recorded', lambda: len(self.takes()) == 1)
        self.assertEqual(self.h.queue_job(job_id)['state'], 'finished')

    # -- S5: the prompt completes while the director is dead ---------------------------
    def test_s5_completion_while_dead_recorded_once(self):
        self.make_project()
        self.h.ctl('mode', mode='run')
        job_id = self.render()
        self.h.wait_until('job running', lambda: (self.h.queue_job(job_id) or {}).get('state') == 'running')
        pid = self.h.stub_state()['running'][0]
        self.h.kill_web()
        self.h.ctl('complete', prompt_id=pid)                     # the take lands while the director is dead
        self.h.restart_web()
        self.h.wait_until('completion recorded on restart', lambda: len(self.takes()) == 1)
        self.assertEqual(self.h.queue_job(job_id)['state'], 'finished')
        self.assertEqual(len(self.takes()), 1)                    # recorded exactly once
        self.assertEqual(self.h.stub_prompt_count(), 1)

    # -- S6: crash inside the result-recording window (raced) ---------------------------
    def test_s6_crash_during_take_recording(self):
        self.h.ctl('mode', mode='auto')             # take hits project.json BEFORE 'finished' hits the db
        for attempt in range(12):
            if attempt:
                self.h.ctl('reset')                 # clean stub, clean prompt count, mode back to auto
            self.make_project()                     # fresh slug so takes never accumulate
            job_id = self.render()
            pj = self.h.projects_root / self.slug / 'project.json'
            deadline = time.time() + 15
            while time.time() < deadline:           # tight poll, then kill immediately: we want the
                try:                                # sliver between the take write and the finish write
                    if json.loads(pj.read_text())['scenes'][0].get('takes'):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.002)
            else:
                self.fail('the take was never recorded while the director was still alive')
            self.h.kill_web()                       # race the 'finished' write
            row = self.h.db_row(job_id)
            if row and row['state'] in RECOVER_STATES:      # killed inside the recording window
                self.h.restart_web()
                job = self.h.wait_until('finish after a mid-recording kill', lambda: self.h.queue_job(job_id))
                self.assertEqual(job['state'], 'finished')  # recovery finishes the job
                self.assertEqual(len(self.takes()), 1)      # the take was recorded once, not twice
                self.assertEqual(self.h.stub_prompt_count(), 1)
                return
        self.fail('none of the 12 attempts caught the window between the take write and the '
                  "'finished' write; the crash window was not reproduced")

    # -- S7: enhance chain killed mid-stage, the stage later fails ----------------------
    def test_s7_enhance_killed_midstage_then_fails(self):
        self.make_project()
        self.h.ctl('mode', mode='auto')
        self.render()
        self.h.wait_until('take recorded', lambda: len(self.takes()) == 1)
        self.h.ctl('mode', mode='run')                            # the enhance submit will hang, running
        code, body = self.h.api('POST', f'/api/p/{self.slug}/enhance', {'scene': self.s1})
        self.assertEqual(code, 200, body)
        enhance_id = self.new_job_id('enhance')
        self.h.wait_until('enhance submitted', lambda: self.h.stub_prompt_count() == 2)
        self.h.kill_web()
        pid = self.h.stub_state()['running'][0]
        self.h.ctl('fail', prompt_id=pid, message='CUDA out of memory')
        self.h.restart_web()
        job = self.h.wait_until('enhance verdict', lambda: self.h.queue_job(enhance_id))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('CUDA out of memory', job['error'])         # backend error survives the restart
        self.assertEqual(self.h.stub_prompt_count(), 2)           # nothing was resubmitted
        self.h.wait_until('enhance temp cleaned',
                          lambda: not list((self.h.comfy_root / 'input/director/enhance-tmp').glob('*')))
        code, _ = self.h.api('POST', f'/api/p/{self.slug}/prompt', {'scene': self.s1})
        self.assertNotEqual(code, 409, 'a failed enhance must free the scene and the take')
        self.assertEqual(len(self.takes()), 1)                    # the render take is intact

    # -- S8: render-final killed before finishing stages exist --------------------------
    def test_s8_render_final_kill_before_finish_stages(self):
        self.make_project()
        self.h.ctl('mode', mode='run-noresp')
        job_id = self.render(preset='final')                      # render-final chains finishing stages
        self.h.wait_until('main prompt queued', lambda: self.h.stub_state()['running'])
        pid = self.h.stub_state()['running'][0]
        self.h.kill_web()
        self.h.ctl('complete', prompt_id=pid)                     # the main result exists in history
        self.h.restart_web()
        self.h.wait_until('main take recorded', lambda: len(self.takes()) == 1)
        job = self.h.queue_job(job_id)
        self.assertEqual(job['state'], 'finished')
        self.assertIn('finishing stages had not been submitted', job['error'])
        time.sleep(1.5)                                           # a reconcile pass: resubmit would show here
        self.assertEqual(self.h.stub_prompt_count(), 1)           # no automatic downstream GPU work
        self.assertEqual(self.h.stub_state()['running'], [])

    # -- S9: backend gone at restart, then a fresh backend --------------------------------
    def test_s9_backend_gone_then_fresh_backend(self):
        self.make_project()
        self.h.ctl('mode', mode='run')
        job_id = self.render()
        self.h.wait_until('job running', lambda: (self.h.queue_job(job_id) or {}).get('state') == 'running')
        self.h.kill_web()
        self.h.kill_stub()                                        # ComfyUI itself is down at restart
        self.h.restart_web()
        job = self.h.queue_job(job_id)
        self.assertIsNotNone(job, 'the unfinished job must be back in the queue view')
        self.assertEqual(job['state'], 'unknown')
        self.assertTrue(job['recovering'])
        self.assertTrue(job['error'].startswith('ComfyUI not reachable yet'), job['error'])
        row = self.h.db_row(job_id)
        self.assertEqual(row['state'], 'running')                 # row untouched while unreachable
        self.assertIsNotNone(row['recovering_at'], 'unreachable jobs are marked, not verdicted')
        self.h.start_stub(port=self.h.stub_port)                  # same port, backend without memories
        job = self.h.wait_until('the fresh backend answers the reconcile pass', lambda: (
            lambda j: j if 'no record' in (j.get('error') or '') else None)(self.h.queue_job(job_id)))
        self.assertEqual(job['state'], 'unknown')
        self.assertIn('ComfyUI has no record of this job', job['error'])
        self.assertFalse(job['recovering'])
        self.assertEqual(len(self.takes()), 0)                    # history was empty: no take invented
        self.assertEqual(self.h.stub_prompt_count(), 0)           # the fresh backend saw no submit
        self.h.kill_web()                                         # restart AGAIN on the recovered state
        self.h.kill_stub()                                        # nothing may outlive its harness
        self.h.start_stub(port=self.h.stub_port)
        self.h.restart_web()
        job = self.h.wait_until('second restart reaches the same verdict', lambda: (
            lambda j: j if j['state'] == 'unknown' and 'no record' in (j.get('error') or '') else None)(
            self.h.queue_job(job_id)))
        self.assertEqual(len(self.takes()), 0)
        self.assertEqual(self.h.stub_prompt_count(), 0)

    # -- S10: finished jobs: recovery is a no-op, twice -----------------------------------
    def test_s10_finished_restart_is_a_noop(self):
        self.make_project()
        self.h.ctl('mode', mode='auto')
        job_id = self.render()
        self.h.wait_until('job finished', lambda: (self.h.queue_job(job_id) or {}).get('state') == 'finished')
        for restart in (1, 2):
            self.h.kill_web()
            self.h.restart_web()
            job = self.h.queue_job(job_id)
            self.assertEqual(job['state'], 'finished', f'restart {restart} churned a finished job')
            self.assertEqual(len(self.takes()), 1)
            self.assertEqual(self.h.stub_prompt_count(), 1)
            self.assertEqual(self.h.stub_state()['running'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
