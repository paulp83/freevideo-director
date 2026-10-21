#!/usr/bin/env python3
"""Startup reconciliation and reattachment (recover_jobs): the M4 Step 2 rules.

Mocked: the same stub ComfyUI as tests/test_jobs.py -- no real ComfyUI, ffmpeg or GPU.
Real: sqlite (the durable rows a dead director leaves behind), threads, projects.mutate
recording, and web.py's state machine. Run from the repo root:
python3 -m unittest discover tests

Every scenario below starts from rows in jobs_db and an EMPTY web.JOBS -- exactly the
state after a director restart. The invariant across all of them: the stub never receives
a POST /prompt. Recovery may adopt, settle from history, or mark unknown; it never
resubmits."""
import json
import shutil
import tempfile
import threading
import time
import unittest
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
PLAN = test_jobs.PLAN


class Recovery(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-recovery-'))
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
        self.slug = projects.create('Recovery film', 'nothing', 30, '16:9', [])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]
        def give_prompts(p):
            for s in p['scenes']:
                s['prompt'] = 'a cat walks left'
        projects.mutate(self.slug, give_prompts)

    def tearDown(self):
        for t in threading.enumerate():              # never let a resume thread outlive its test
            if getattr(t, '_target', None) is web.run_job:
                t.join(20)
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- fixtures

    def seed_row(self, state, kind='render-draft', pids=(), holds=None, run=None,
                 take_id=None, correlation=None, scene=None, workdir=None):
        """A row written by a director that died: the store has it, the process does not."""
        Recovery.counter += 1
        job = {'id': f'{0xa000 + Recovery.counter:012x}', 'kind': kind,
               'created': time.time() - 60, 'started': time.time() - 55, 'ended': None,
               'error': None, 'done': False, 'state': state, 'deadline': None, 'stages': [],
               'take': ({'run': run} if run else None), 'final': None,
               'prompt_ids': list(pids), 'cancel': None, 'project': self.slug,
               'scene': self.ids[0] if scene is None else scene,
               'holds': [list(k) for k in (holds if holds is not None
                                           else [web.scene_key(self.slug, self.ids[0])])],
               'correlation': correlation, 'workdir': str(workdir) if workdir else None,
               'take_id': take_id, 'resolved': None}
        jobs_db.save(job)
        return job['id']

    def take_dir(self, name):
        run = web.COMFY_OUTPUT / 'director' / name
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        return str(run)

    def takes(self, scene=None):
        p = projects.load(self.slug)
        return p['scenes'][projects.scene_index(p, self.ids[0] if scene is None else scene)].get('takes') or []

    def wait_for(self, cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def post(self, path):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{srv.server_address[1]}{path}',
                                         data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json'})
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        finally:
            srv.shutdown()
            srv.server_close()

    # ---------------------------------------------------------------- reattach

    def test_a_running_prompt_still_in_comfyui_is_adopted_not_resubmitted(self):
        StubComfy.mode = 'hang'
        StubComfy.running_ids = ['rp-1']
        take_id = projects.new_take_id()
        jid = self.seed_row('running', pids=['rp-1'], take_id=take_id)
        summary = web.recover_jobs()
        self.assertEqual(summary['reattached'], 1)
        self.assertEqual(StubComfy.submits, 0)                    # watching, never submitting
        job = web.JOBS[jid]
        self.assertTrue(self.wait_for(lambda: job['state'] in ('submitted', 'running')))
        self.assertIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)
        StubComfy.mode = 'complete'
        self.assertTrue(self.wait_for(lambda: job['state'] == 'finished'))
        self.assertEqual(len(self.takes()), 1)
        take = self.takes()[0]
        self.assertEqual((take['id'], take['job']), (take_id, jid))
        self.assertEqual(jobs_db.get(jid)['state'], 'finished')
        self.assertIsNotNone(projects.read_job(self.slug, jid)[1])

    def test_a_submitted_row_whose_prompt_is_still_queued_is_adopted(self):
        StubComfy.mode = 'complete'
        StubComfy.pending = ['sp-1']
        jid = self.seed_row('submitting', pids=['sp-1'])
        summary = web.recover_jobs()
        self.assertEqual(summary['reattached'], 1)
        self.assertEqual(StubComfy.submits, 0)
        self.assertTrue(self.wait_for(lambda: web.JOBS[jid]['state'] == 'finished'))
        self.assertEqual(len(self.takes()), 1)
        self.assertEqual(jobs_db.get(jid)['state'], 'finished')

    def test_an_ongoing_enhance_is_adopted_and_its_final_recorded(self):
        run = self.take_dir('enh-run')
        workdir = self.root / 'enh-tmp'
        workdir.mkdir()
        (workdir / 'raw.mkv').write_bytes(b'mkv')
        tid = projects.new_take_id()
        def record_take(p):                                   # the take being enhanced
            s = p['scenes'][projects.scene_index(p, self.ids[0])]
            s['takes'].append({'id': tid, 'run': run, 'path': f'{run}/video.mp4',
                               'url': None, 'final': None, 'final_url': None})
        projects.mutate(self.slug, record_take)
        StubComfy.mode = 'hang'
        StubComfy.running_ids = ['en-1']
        jid = self.seed_row('running', kind='enhance', pids=['en-1'], run=run,
                            holds=[web.take_key(run)], take_id=tid, workdir=workdir)
        summary = web.recover_jobs()
        self.assertEqual(summary['reattached'], 1)
        self.assertEqual(StubComfy.submits, 0)
        job = web.JOBS[jid]
        StubComfy.mode = 'complete'
        self.assertTrue(self.wait_for(lambda: job['state'] == 'finished'))
        self.assertIsNotNone(job['final'])
        take = self.takes()[0]
        self.assertEqual(take['final'], job['final']['path'])
        self.assertTrue(take['final_url'].startswith(self.stub_url))
        self.assertIsNone(job['workdir'])                      # the raw temp dir is cleaned up
        self.assertFalse(workdir.exists())
        self.assertEqual(jobs_db.get(jid)['state'], 'finished')

    # ---------------------------------------------------------------- settle from history

    def test_a_prompt_that_completed_while_the_director_was_gone_is_recorded(self):
        StubComfy.mode = 'complete'                            # queue empty; history has it done
        take_id = projects.new_take_id()
        jid = self.seed_row('running', pids=['cp-1'], take_id=take_id)
        summary = web.recover_jobs()
        self.assertEqual(summary['recorded'], 1)
        self.assertEqual(StubComfy.submits, 0)
        self.assertEqual(web.JOBS[jid]['state'], 'finished')
        self.assertEqual(len(self.takes()), 1)
        self.assertEqual(self.takes()[0]['id'], take_id)
        self.assertEqual(jobs_db.get(jid)['state'], 'finished')
        again = web.recover_jobs()                             # a second pass changes nothing
        self.assertEqual({k: v for k, v in again.items() if v}, {})
        self.assertEqual(len(self.takes()), 1)

    def test_a_take_already_recorded_is_never_recorded_twice(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')   # live, before the restart
        self.assertEqual(job['state'], 'finished')
        first = self.takes()[0]
        jobs_db.save(job, state='running', take_id=first['id'],
                     prompt_ids=job['prompt_ids'], correlation='client-supplied')
        web.JOBS.clear()                                       # the director died mid-thought
        summary = web.recover_jobs()
        self.assertEqual(summary['recorded'], 1)
        self.assertEqual(self.takes(), [first])                # not one more, not one changed
        self.assertEqual(StubComfy.submits, 1)                 # only the original live submit

    def test_a_failed_prompt_fails_the_job_with_comfyui_s_own_message(self):
        StubComfy.mode = 'failed'
        jid = self.seed_row('submitted', pids=['f-1'])
        summary = web.recover_jobs()
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(web.JOBS[jid]['state'], 'failed')
        self.assertIn('CUDA out of memory', web.JOBS[jid]['error'])
        self.assertEqual(jobs_db.get(jid)['state'], 'failed')
        self.assertEqual(web.RESERVES, {})                     # failed: the scene is free
        self.assertEqual(self.takes(), [])

    def test_a_final_render_whose_finish_never_started_records_the_take_honestly(self):
        StubComfy.mode = 'complete'
        jid = self.seed_row('running', kind='render-final', pids=['rf-1'])
        summary = web.recover_jobs()
        self.assertEqual(summary['recorded'], 1)
        job = web.JOBS[jid]
        self.assertEqual(job['state'], 'finished')
        self.assertIn('finishing stages had not been submitted', job['error'])
        self.assertEqual(len(self.takes()), 1)                 # the raw take is still recorded
        self.assertEqual(StubComfy.bodies, [])                 # no finishing prompt was ever POSTed

    # ---------------------------------------------------------------- unknown verdicts

    def test_a_vanished_prompt_becomes_unknown_and_keeps_the_scene(self):
        StubComfy.mode = 'gone'                                # neither queue nor history
        jid = self.seed_row('running', pids=['g-1'])
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)
        self.assertEqual(StubComfy.submits, 0)
        job = web.JOBS[jid]
        self.assertEqual(job['state'], 'unknown')
        self.assertIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)
        self.assertFalse(job['retry_allowed'])                 # an acknowledged submit: no honest Retry
        self.assertFalse(jobs_db.get(jid)['retry_allowed'])
        self.assertIn('never resubmitted', job['error'])

    def test_an_unconfirmed_submit_is_unknown_and_offers_retry(self):
        StubComfy.mode = 'gone'
        jid = self.seed_row('submitting', pids=['ghost-1'])
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)
        job = web.JOBS[jid]
        self.assertEqual(job['state'], 'unknown')
        self.assertIn('the submit was not confirmed', job['error'])
        self.assertTrue(job['retry_allowed'])                  # the POST may never have landed
        self.assertTrue(jobs_db.get(jid)['retry_allowed'])
        self.assertTrue(web.public(job)['retry_allowed'])      # the routes expose the flag
        self.assertIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)

    def test_a_row_with_an_acknowledged_server_assigned_id_offers_no_retry(self):
        StubComfy.mode = 'gone'
        jid = self.seed_row('running', pids=['sa-1'], correlation='server-assigned')
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)
        self.assertFalse(web.JOBS[jid]['retry_allowed'])       # ComfyUI had it; purge is possible
        self.assertFalse(jobs_db.get(jid)['retry_allowed'])

    def test_a_row_that_never_reached_comfyui_offers_retry(self):
        jid = self.seed_row('queued', pids=())                 # record_submit had not written ids
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)
        self.assertEqual(StubComfy.submits, 0)
        job = web.JOBS[jid]
        self.assertIn('before anything reached ComfyUI', job['error'])
        self.assertTrue(job['retry_allowed'])
        self.assertTrue(jobs_db.get(jid)['retry_allowed'])
        self.assertIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)

    # ---------------------------------------------------------------- unreachable ComfyUI

    def test_an_unreachable_comfyui_changes_nothing_until_it_answers(self):
        StubComfy.mode = 'hang'
        StubComfy.running_ids = ['ur-1']                       # ComfyUI still holds the prompt
        jid = self.seed_row('running', pids=['ur-1'])
        summary = web.recover_jobs(comfy='http://127.0.0.1:1') # the server was not up (yet)
        self.assertEqual(summary['recovering'], 1)
        row = jobs_db.get(jid)
        self.assertEqual(row['state'], 'running')              # state untouched...
        self.assertIsNotNone(row['recovering_at'])             # ... only the marker moved
        self.assertEqual(web.JOBS, {})                         # nothing adopted
        summary = web.recover_jobs()                           # ComfyUI answers on the next pass
        self.assertEqual(summary['reattached'], 1)
        self.assertIsNone(jobs_db.get(jid)['recovering_at'])
        job = web.JOBS[jid]
        StubComfy.mode = 'complete'
        self.assertTrue(self.wait_for(lambda: job['state'] == 'finished'))
        self.assertEqual(len(self.takes()), 1)

    def test_the_queue_board_shows_waiting_rows_as_unknown(self):
        jid = self.seed_row('running', pids=['q-1'])
        web.recover_jobs(comfy='http://127.0.0.1:1')
        rows = [r for r in web.queue_view()['jobs'] if r['id'] == jid]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['state'], 'unknown')
        self.assertTrue(row['error'].startswith(web.NOT_REACHABLE))
        self.assertTrue(row['recovering'])
        self.assertTrue(row['persisted'])
        self.assertFalse(row['live'])
        self.assertEqual(jobs_db.get(jid)['state'], 'running')  # the store was never touched

    # ---------------------------------------------------------------- routes after a restart

    def test_a_cancel_of_a_reattached_job_targets_only_its_own_prompt(self):
        StubComfy.mode = 'hang'
        StubComfy.pending = ['mine']
        StubComfy.running_ids = ['someone-else']               # an unrelated prompt occupies ComfyUI
        jid = self.seed_row('running', pids=['mine'])
        self.assertEqual(web.recover_jobs()['reattached'], 1)
        r = self.post(f'/api/job/{jid}/cancel')
        self.assertTrue(r['proved'])
        job = web.JOBS[jid]
        self.assertTrue(self.wait_for(lambda: job['state'] == 'cancelled'))
        self.assertEqual(StubComfy.deletes, [['mine']])        # exactly our queued prompt
        self.assertEqual(StubComfy.interrupts, 0)              # none of ours was executing
        self.assertEqual(StubComfy.running_ids, ['someone-else'])

    def test_a_persisted_unknown_is_adopted_and_later_reconciles_to_recorded(self):
        StubComfy.mode = 'gone'
        jid = self.seed_row('unknown', pids=['un-1'])          # unresolved: still holding the scene
        summary = web.recover_jobs()
        self.assertEqual(summary['unknown'], 1)                # adopted as unknown, nothing else
        self.assertIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)
        StubComfy.mode = 'complete'                            # ComfyUI's history proves it finished
        r = self.post(f'/api/job/{jid}/reconcile')
        self.assertEqual(r['proved'], 'finished')
        self.assertEqual(len(self.takes()), 1)                 # recorded late, exactly once
        self.assertNotIn(web.scene_key(self.slug, self.ids[0]), web.RESERVES)
        r = self.post(f'/api/job/{jid}/reconcile')             # a second proof changes nothing
        self.assertEqual(len(self.takes()), 1)


if __name__ == '__main__':
    unittest.main()
