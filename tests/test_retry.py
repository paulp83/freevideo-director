#!/usr/bin/env python3
"""Explicit Retry of a crash-window job (M4 Step 3): the recorded request goes out again --
once, only when the submit provably never landed, and only after ComfyUI is re-checked.

Mocked: the same stub ComfyUI as tests/test_jobs.py -- no real ComfyUI, ffmpeg or GPU.
Real: sqlite rows, threads, projects.mutate recording and web.py's state machine. Run from
the repo root: python3 -m unittest discover tests

Every scenario starts the way the crash window really looks: a dead director leaves durable
rows, recover_jobs() turns them into unknown jobs holding their scene, and only then does the
user press Retry. The invariant across the refusals: StubComfy.submits stays 0 -- a refused
retry must never have submitted anything."""
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import enhance
import jobs_db
import project as projects
import stats
import web
import test_jobs                      # the stub ComfyUI and the PLAN fixture

StubComfy = test_jobs.StubComfy


class Retry(unittest.TestCase):
    counter = 0                        # hex job ids; projects.job_path accepts them

    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(('127.0.0.1', 0), StubComfy)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.stub_url = f'http://127.0.0.1:{cls.stub.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-retry-'))
        StubComfy.mode, StubComfy.submits = 'complete', 0
        StubComfy.reset()
        self.saved = (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
                      web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS)
        projects.ROOT = self.root
        stats.FILE = self.root / 'stats.json'
        jobs_db.FILE = self.root / 'jobs.db'
        enhance.COMFY = web.COMFY = self.stub_url
        enhance.COMFY_ROOT = self.root / 'comfy'
        web.COMFY_OUTPUT = self.root / 'comfy' / 'output'
        web.JOB_TIMEOUT = 120.0
        web.POLL_SECONDS = 0.05
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        self.slug = projects.create('Retry film', 'nothing', 30, '16:9', [])['slug']
        def mutate(p):
            projects.apply_storyboard(p, test_jobs.PLAN)
            for s in p['scenes']:
                s['prompt'] = 'a cat walks left'
        projects.mutate(self.slug, mutate)
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]

    def tearDown(self):
        for t in threading.enumerate():              # never let a retry thread outlive its test
            if getattr(t, '_target', None) is web.run_job:
                t.join(20)
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- fixtures

    def seed_row(self, state, kind='render-draft', pids=(), run=None, take_id='t-old',
                 correlation=None, scene=None):
        """A row written by a director that died mid-submit: the store has it, the process does not."""
        Retry.counter += 1
        job = {'id': f'{0xb000 + Retry.counter:012x}', 'kind': kind,
               'created': time.time() - 60, 'started': time.time() - 55, 'ended': None,
               'error': None, 'done': False, 'state': state, 'deadline': None, 'stages': [],
               'take': ({'run': run} if run else None), 'final': None,
               'prompt_ids': list(pids), 'cancel': None, 'project': self.slug,
               'scene': self.ids[0] if scene is None else scene,
               'holds': [list(web.scene_key(self.slug, self.ids[0] if scene is None else scene))],
               'correlation': correlation, 'workdir': None,
               'take_id': take_id, 'resolved': None}
        jobs_db.save(job)
        return job['id']

    def seed_snapshot(self, jid, kind='render-draft', prompt='the recorded prompt', seed=7):
        """The request record the dead director wrote before its submit -- the fields a Retry
        replays, deliberately different from what the scene says NOW."""
        projects.write_job_snapshot(self.slug, {
            'id': jid, 'kind': kind, 'created': time.time() - 60, 'project': self.slug,
            'scene': str(self.ids[0]), 'take_id': 't-old', 'preset': 'draft', 'variant': None,
            'mode': 't2va', 'prompt': prompt, 'seed': seed, 'width': 1024, 'height': 576,
            'seconds': 5, 'two_pass': True, 'refs': [], 'first': None, 'loras': [], 'graph': {},
            'director': {'model': 'some-model', 'skill_revision': 'r'}, 'instructions': '',
            'finishing': None})

    def seed_enhance_snapshot(self, jid, run):
        projects.write_job_snapshot(self.slug, {
            'id': jid, 'kind': 'enhance', 'created': time.time() - 60, 'project': self.slug,
            'scene': str(self.ids[0]), 'take_id': 't-old',
            'source': {'take': 'tk-1', 'run': run, 'provenance': 'unknown'},
            'finishing': None, 'instructions': ''})

    def take_dir(self, name):
        run = web.COMFY_OUTPUT / 'director' / name
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        return str(run)

    def wait_for(self, cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def post(self, path):
        """Returns (code, body) -- a 409 refusal carries the reason, so it must not raise."""
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{srv.server_address[1]}{path}',
                                         data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json'})
            try:
                r = urllib.request.urlopen(req, timeout=10)
                return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        finally:
            srv.shutdown()
            srv.server_close()

    def crashed_unknown(self, **row_kw):
        """recover_jobs() on a seeded row: the post-crash unknown the Retry button offers on."""
        jid = self.seed_row(**row_kw)
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)
        return jid

    def takes(self):
        p = projects.load(self.slug)
        return p['scenes'][projects.scene_index(p, self.ids[0])].get('takes') or []

    # ---------------------------------------------------------------- the honest happy path

    def test_retry_resubmits_the_recorded_request_exactly_once(self):
        StubComfy.mode = 'complete'
        jid = self.crashed_unknown(state='submitting', pids=())
        self.seed_snapshot(jid)
        job = web.JOBS[jid]
        self.assertTrue(job['retry_allowed'])
        key = web.scene_key(self.slug, str(self.ids[0]))
        self.assertIn(key, web.RESERVES)                       # still held while unknown
        code, d = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 200)
        self.assertEqual(d['retry_of'], jid)
        self.assertIn('may still be executing', d['warning'])  # the never-answered variant
        new_id = d['job']['id']
        self.assertEqual(d['job']['retry_of'], jid)
        self.assertTrue(self.wait_for(lambda: web.JOBS[new_id]['done']))
        self.assertEqual(web.JOBS[new_id]['state'], 'finished', web.JOBS[new_id]['error'])
        self.assertEqual(StubComfy.submits, 1)                 # exactly one POST /prompt: the retry
        old = jobs_db.get(jid)
        self.assertEqual(old['resolved']['verdict'], 'retried')
        self.assertEqual(old['resolved']['by'], 'user')        # the user asked for it, we did not
        self.assertEqual(old['retried_as'], new_id)
        self.assertEqual(jobs_db.find_retry(jid)['id'], new_id)
        snap, _ = projects.read_job(self.slug, new_id)
        self.assertEqual(snap['retry_of'], jid)                # the new job names what it replays
        self.assertEqual(len(self.takes()), 1)                 # the retry's take lands normally
        self.assertEqual(web.RESERVES, {})                     # done: the scene is free again
        rows = {r['id']: r for r in web.queue_view()['jobs']}
        self.assertEqual(rows[new_id]['retry_of'], jid)
        self.assertEqual(rows[jid]['retried_as'], new_id)

    def test_the_retry_replays_recorded_values_not_todays_scene(self):
        StubComfy.mode = 'complete'
        jid = self.crashed_unknown(state='submitting', pids=())
        self.seed_snapshot(jid, prompt='the recorded prompt', seed=7)
        def retune(p):                                       # the user edits the scene after the crash
            p['scenes'][0]['prompt'] = 'a dog runs right'
            p['scenes'][0]['seed'] = 99
        projects.mutate(self.slug, retune)
        code, d = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 200)
        new_id = d['job']['id']
        self.assertTrue(self.wait_for(lambda: web.JOBS[new_id]['done']))
        body = json.dumps(StubComfy.bodies[-1])
        self.assertIn('the recorded prompt', body)             # what was asked for the first time
        self.assertNotIn('a dog runs right', body)             # never today's settings
        self.assertIn('"seed": 7', body)

    def test_a_second_retry_names_the_first_one_without_submitting_again(self):
        StubComfy.mode = 'complete'
        jid = self.crashed_unknown(state='submitting', pids=())
        self.seed_snapshot(jid)
        code, d = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: web.JOBS[d['job']['id']]['done']))
        again = web.retry_job(web.JOBS[jid])                   # past the route's resolved check:
        self.assertTrue(again['already'])                      # the store link answers, not a submit
        self.assertEqual(again['job']['id'], d['job']['id'])
        self.assertEqual(StubComfy.submits, 1)                 # still exactly one
        code, e = self.post(f'/api/job/{jid}/retry')           # the route refuses a resolved row
        self.assertEqual(code, 400)
        self.assertIn('unknown', e['error'])

    # ---------------------------------------------------------------- refusals: nothing submitted

    def test_no_retry_for_a_submit_comfyui_had_acknowledged(self):
        StubComfy.mode = 'gone'                                # vanished without a trace
        jid = self.crashed_unknown(state='running', pids=['ack-1'])
        self.assertFalse(web.JOBS[jid]['retry_allowed'])       # a purge cannot be told from a loss
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 409)
        self.assertIn('double the GPU work', e['error'])
        self.assertEqual(StubComfy.submits, 0)
        self.assertIsNone(jobs_db.get(jid)['resolved'])        # nothing moved at all

    def test_retry_adopts_a_prompt_that_turned_out_to_still_be_running(self):
        StubComfy.mode = 'gone'
        jid = self.crashed_unknown(state='submitting', pids=['cid-1'],
                                   correlation='client-supplied')
        self.assertTrue(web.JOBS[jid]['retry_allowed'])        # a client id ComfyUI never echoed
        StubComfy.mode = 'hang'
        StubComfy.running_ids = ['cid-1']                      # it IS running after all
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 409)
        self.assertIn('reattached', e['error'])
        self.assertEqual(StubComfy.submits, 0)                 # adopted, never duplicated
        live = web.JOBS[jid]
        self.assertFalse(live['done'])                         # reattached: its prompt is polled
        self.assertEqual(live['prompt_ids'], ['cid-1'])
        self.assertIsNone(jobs_db.get(jid)['resolved'])
        StubComfy.mode = 'complete'                          # release the adopted poll early

    def test_retry_refuses_a_finished_original_and_records_it(self):
        jid = self.seed_row('submitting', pids=['fin-1'])
        self.seed_snapshot(jid)
        web.recover_jobs()                                     # completed: the take is recorded
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 400)
        self.assertEqual(len(self.takes()), 1)                 # recovery already recorded it
        self.assertEqual(StubComfy.submits, 0)

    def test_retry_refuses_a_job_still_running_and_unknowns_and_404s(self):
        live = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        code, e = self.post(f'/api/job/{live["id"]}/retry')
        self.assertEqual(code, 409)
        self.assertIn('still running', e['error'])
        run = self.take_dir('run-done')
        done = web.new_job('enhance', ['upscale'], project=self.slug)
        web.run_job(done, web.enhance_job, run)
        self.assertEqual(done['state'], 'finished')
        code, e = self.post(f'/api/job/{done["id"]}/retry')
        self.assertEqual(code, 400)
        code, e = self.post('/api/job/nope000000000000/retry')
        self.assertEqual(code, 404)
        self.assertEqual(StubComfy.submits, 1)             # only the enhance above submitted

    def test_retry_never_submits_when_recorded_inputs_are_gone(self):
        StubComfy.mode = 'complete'
        jid = self.crashed_unknown(state='submitting', pids=())  # no snapshot written at all
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 409)
        self.assertIn('no request record to replay', e['error'])
        self.assertEqual(StubComfy.submits, 0)
        run = self.take_dir('run-vanished')
        jid = self.crashed_unknown(state='submitting', kind='enhance', pids=(), run=run,
                                   scene=self.ids[1])      # its own scene: no hold clash
        self.seed_enhance_snapshot(jid, run)
        shutil.rmtree(Path(run).parent)                        # ComfyUI's output folder pruned
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 409)
        self.assertIn('no longer in the output folder', e['error'])
        self.assertEqual(StubComfy.submits, 0)

    def test_retry_waits_for_comfyui_to_answer_before_anything_moves(self):
        StubComfy.mode = 'complete'
        jid = self.seed_row('submitting', pids=())             # ComfyUI down at startup
        web.recover_jobs(comfy='http://127.0.0.1:1')           # the pass can ask nothing
        self.assertNotIn(jid, web.JOBS)                        # rows wait, unchanged
        web.COMFY = 'http://127.0.0.1:1'                   # ComfyUI dead when Retry is asked
        code, e = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 409)                            # not a 404: the row is answerable
        self.assertIn('not answering', e['error'])
        self.assertEqual(StubComfy.submits, 0)
        self.assertIsNone(jobs_db.get(jid)['resolved'])

    # ---------------------------------------------------------------- the enhance door

    def test_an_enhance_retry_replays_its_recorded_source_take(self):
        StubComfy.mode = 'complete'
        run = self.take_dir('run-e')
        jid = self.crashed_unknown(state='submitting', kind='enhance', pids=(), run=run)
        self.seed_enhance_snapshot(jid, run)
        code, d = self.post(f'/api/job/{jid}/retry')
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: web.JOBS[d['job']['id']]['done']))
        self.assertEqual(web.JOBS[d['job']['id']]['state'], 'finished',
                         web.JOBS[d['job']['id']]['error'])
        self.assertEqual(StubComfy.submits, 1)
        self.assertEqual(jobs_db.find_retry(jid)['id'], d['job']['id'])
        self.assertEqual(jobs_db.get(jid)['resolved']['verdict'], 'retried')

    def test_the_warning_copy_matches_the_evidence(self):
        """retry_warning tells the user what the retry knows and risks -- the exact strings the
        pages show, without racing a live submit."""
        w = web.retry_warning({'prompt_ids': ['cid-9'], 'correlation': 'client-supplied'}, None)
        self.assertIn('no record of prompt cid-9', w)
        self.assertIn('may generate a second take', w)
        w = web.retry_warning({'prompt_ids': [], 'correlation': None}, None)
        self.assertIn('may still be executing', w)
        self.assertIn('second take', w)
        w = web.retry_warning({'prompt_ids': ['x'], 'correlation': 'server-assigned'}, 'failed')
        self.assertIn('original prompt failed', w)
        w = web.retry_warning({'prompt_ids': ['x'], 'correlation': 'server-assigned'}, 'interrupted')
        self.assertIn('interrupted', w)

    # ---------------------------------------------------------------- wiring guard

    def test_retry_is_reachable_only_through_the_route(self):
        """Nothing in web.py calls retry_job() except the /retry route: a retry can never be
        automatic (no reconcile loop, no recovery pass, no cancel path reaches it)."""
        import ast
        tree = ast.parse(Path('web.py').read_text())
        callers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for call in ast.walk(node):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == 'retry_job'):
                        callers.append(node.name)
        self.assertEqual(callers, ['do_POST'])


if __name__ == '__main__':
    unittest.main()
