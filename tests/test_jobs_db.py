#!/usr/bin/env python3
"""The durable job store (jobs_db) and web.py's write-through.

Mocked: the same stub ComfyUI as tests/test_jobs.py -- no real ComfyUI, ffmpeg or GPU is
involved. Real: sqlite (WAL, the versioned migration), threads, and web.py's full job
machinery. Run from the repo root: python3 -m unittest discover tests

The store lives in a temp dir per test (jobs_db.FILE / an explicit init path), never in the
repo, exactly like stats.FILE elsewhere."""
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import enhance
import jobs_db
import project as projects
import stats
import web
import test_jobs                      # the stub ComfyUI and the PLAN fixture


def make_job(i, state, at=None, id_=None):
    """A job dict shaped like web.new_job's, for direct store tests."""
    at = at if at is not None else time.time()
    return {'id': id_ or f'{i:012x}', 'kind': 'render', 'created': at - 4, 'started': at - 3,
            'ended': at - 1 if state in jobs_db.TERMINAL else None, 'error': None,
            'done': state in jobs_db.TERMINAL, 'state': state, 'deadline': None,
            'stages': [{'key': 'render', 'label': 'Render', 'state': 'done'}],
            'take': None, 'final': None, 'prompt_ids': [f'p{i}'], 'cancel': None,
            'project': 'film', 'scene': 'sc1', 'holds': [('scene', 'film:sc1')],
            'resolved': ({'by': 'user', 'verdict': 'stopped', 'at': at} if state == 'unknown' else None)}


class Store(unittest.TestCase):
    """The store alone: schema, migration, row semantics, retention."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='jobsdb-'))
        self.db = self.root / 'jobs.db'
        jobs_db.init(self.db)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def rows(self, path=None):
        con = sqlite3.connect(path or self.db)
        try:
            return con.execute('SELECT id, state FROM jobs ORDER BY id').fetchall()
        finally:
            con.close()

    def test_versioned_database_created(self):
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 1)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        self.assertIn('jobs', names)
        self.assertEqual(jobs_db.list_jobs(), [])

    def test_init_is_idempotent(self):
        jobs_db.init(self.db)
        jobs_db.init(self.db)
        self.assertEqual(self.rows(), [])

    def test_migrates_an_older_database(self):
        path = self.root / 'old.db'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE leftovers (x)')          # whatever else the file held stays
        con.execute('PRAGMA user_version = 0')
        con.commit()
        con.close()
        jobs_db.init(path)
        con = sqlite3.connect(path)
        self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 1)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        self.assertIn('jobs', names)
        self.assertIn('leftovers', names)

    def test_save_get_roundtrip(self):
        job = make_job(7, 'unknown')
        jobs_db.save(job, backend='127.0.0.1:8190',
                     snapshot_path='projects/film/jobs/000000000007.json',
                     result_path='projects/film/jobs/000000000007.result.json')
        row = jobs_db.get('000000000007')
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['kind'], 'render')
        self.assertEqual(row['project'], 'film')
        self.assertEqual(row['scene'], 'sc1')
        self.assertEqual(row['prompt_ids'], ['p7'])
        self.assertEqual(row['holds'], [['scene', 'film:sc1']])          # tuples come back as lists
        self.assertEqual(row['resolved'], {'by': 'user', 'verdict': 'stopped', 'at': row['resolved_at']})
        self.assertEqual(row['backend'], '127.0.0.1:8190')
        self.assertEqual(row['snapshot_path'], 'projects/film/jobs/000000000007.json')
        self.assertEqual(row['result_path'], 'projects/film/jobs/000000000007.result.json')
        self.assertIsNone(jobs_db.get('ffffffffffff'))

    def test_save_replaces_the_whole_row(self):
        job = make_job(7, 'queued')
        jobs_db.save(job, backend='127.0.0.1:8190')
        job['state'] = 'failed'
        job['error'] = 'ComfyUI went away'
        job['prompt_ids'] = ['p7', 'p8']
        jobs_db.save(job, backend='127.0.0.1:8190')
        row = jobs_db.get('000000000007')
        self.assertEqual((row['state'], row['error'], row['prompt_ids']),
                         ('failed', 'ComfyUI went away', ['p7', 'p8']))

    def test_record_submit_marks_the_row_submitter(self):
        job = make_job(9, 'preparing')
        jobs_db.record_submit(job, 'b7f3-uuid-stub', backend='127.0.0.1:8190')
        row = jobs_db.get('000000000009')
        self.assertEqual(row['state'], 'submitting')                    # persisted-only state
        self.assertEqual(row['prompt_ids'], ['p9', 'b7f3-uuid-stub'])   # the submitted id lands in the row
        self.assertEqual(row['correlation'], 'client-supplied')

    def test_record_submit_raises_when_the_store_refuses(self):
        unwritable = self.root / 'not-a-database'
        unwritable.mkdir()
        saved, jobs_db.FILE = jobs_db.FILE, unwritable
        try:
            with self.assertRaises(jobs_db.JobDbError):
                jobs_db.record_submit(make_job(1, 'preparing'), 'pid-1')
        finally:
            jobs_db.FILE = saved

    def test_forget_removes_the_row(self):
        jobs_db.save(make_job(3, 'queued'))
        self.assertIsNotNone(jobs_db.get('000000000003'))
        jobs_db.forget('000000000003')
        self.assertIsNone(jobs_db.get('000000000003'))
        jobs_db.forget('000000000003')                                  # idempotent

    def test_list_filters_project_state_window_and_order(self):
        now = time.time()
        jobs_db.save(make_job(1, 'finished', at=now - 10))
        jobs_db.save(make_job(2, 'failed', at=now - 4000))
        jobs_db.save(make_job(3, 'running', at=now - 5))
        old = make_job(4, 'finished', at=now - 20)
        old['project'] = 'other-film'
        jobs_db.save(old)
        self.assertEqual([r['id'] for r in jobs_db.list_jobs()],
                         ['000000000003', '000000000001', '000000000004', '000000000002'])
        self.assertEqual([r['id'] for r in jobs_db.list_jobs(states=('finished', 'failed'))],
                         ['000000000001', '000000000004', '000000000002'])
        self.assertEqual([r['id'] for r in jobs_db.list_jobs(project='film')],
                         ['000000000003', '000000000001', '000000000002'])
        self.assertEqual([r['id'] for r in jobs_db.list_jobs(since=now - 100)],
                         ['000000000003', '000000000001', '000000000004'])   # in-window rows; the ancient one out
        self.assertEqual([r['id'] for r in jobs_db.list_jobs(limit=2)],
                         ['000000000003', '000000000001'])

    def test_prune_keeps_everything_inside_the_window(self):
        now = time.time()
        for i in range(600):                                            # 600 terminal rows, all recent
            jobs_db.save(make_job(i, 'finished', at=now - i))
        jobs_db.prune()
        self.assertEqual(len(self.rows()), 600)                         # 600 > 500, but all inside 30 days

    def test_prune_caps_old_terminal_rows_at_the_last_many(self):
        now = time.time()
        for i in range(600):                                            # older than the window: cap applies
            jobs_db.save(make_job(i, 'finished', at=now - 31 * 86400 - i))
        for i in range(10):
            jobs_db.save(make_job(i, 'failed', at=now - 45 * 86400 - i, id_=f'old{i:09d}'))
        jobs_db.save(make_job(0, 'submitting', at=now - 45 * 86400, id_='old999999999'))
        jobs_db.prune()
        kept = {r[0] for r in self.rows()}
        self.assertEqual(len(kept), 501)                                # the last 500 + the submitting row
        self.assertIn('old999999999', kept)                             # unknown work is NEVER pruned away
        self.assertIn(f'{0:012x}', kept)                                # the newest survives
        self.assertIn(f'{499:012x}', kept)                              # rank 500 survives
        self.assertNotIn(f'{500:012x}', kept)                           # rank 501 is gone
        self.assertNotIn('old000000000', kept)                          # the oldest terminal rows are gone
        self.assertNotIn('old000000000', kept)                          # ancient terminal rows are gone

    def test_prune_never_touches_unfinished_rows(self):
        old = time.time() - 365 * 86400
        for state in ('queued', 'preparing', 'submitted', 'running'):
            jobs_db.save(make_job(0, state, at=old, id_=f'live{state}000'))
        jobs_db.prune()
        self.assertEqual(len(self.rows()), 4)


class WriteThrough(unittest.TestCase):
    """web.py's machinery writing every job through, and the queue board reading it back."""

    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(('127.0.0.1', 0), test_jobs.StubComfy)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.stub_url = f'http://127.0.0.1:{cls.stub.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-jobsdb-'))
        test_jobs.StubComfy.mode, test_jobs.StubComfy.submits = 'complete', 0
        test_jobs.StubComfy.reset()
        self.saved = (projects.ROOT, stats.FILE, enhance.COMFY, enhance.COMFY_ROOT,
                      web.COMFY, web.COMFY_OUTPUT, web.POLL_SECONDS, jobs_db.FILE)
        projects.ROOT = self.root
        stats.FILE = self.root / 'stats.json'
        enhance.COMFY = web.COMFY = self.stub_url
        enhance.COMFY_ROOT = self.root / 'comfy'
        web.COMFY_OUTPUT = self.root / 'comfy' / 'output'
        web.POLL_SECONDS = 0.05
        jobs_db.FILE = self.root / 'jobs.db'
        jobs_db.init()
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        self.slug = projects.create('Jobsdb film', 'nothing', 30, '16:9', [])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, test_jobs.PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]

        def give_prompts(p):
            for s in p['scenes']:
                s['prompt'] = 'a cat walks left'
        projects.mutate(self.slug, give_prompts)

    def tearDown(self):
        (projects.ROOT, stats.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.POLL_SECONDS, jobs_db.FILE) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_finished_render_is_durable_with_its_full_row(self):
        seen = []
        real_transition = web.transition

        def spy(job, to):                                 # what the store holds after each move
            real_transition(job, to)
            seen.append(jobs_db.get(job['id'])['state'])
        web.transition = spy
        try:
            job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
            web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        finally:
            web.transition = real_transition
        self.assertEqual(job['state'], 'finished')
        self.assertEqual(seen, ['preparing', 'submitted', 'running', 'finished'])
        row = jobs_db.get(job['id'])
        self.assertEqual(row['state'], 'finished')
        self.assertEqual(row['kind'], 'render-draft')
        self.assertEqual(row['project'], self.slug)
        self.assertEqual(row['scene'], self.ids[0])
        self.assertEqual(row['backend'], self.stub_url.split('://', 1)[1])
        self.assertEqual(row['prompt_ids'], job['prompt_ids'])
        self.assertEqual(row['take_id'], job.get('take_id'))
        self.assertRegex(row['take_id'] or '', r'^[0-9a-f]{16}$')
        self.assertIsNone(row['error'])
        self.assertTrue(row['created'] and row['started'] and row['ended'])
        self.assertTrue(row['snapshot_path'].startswith(str(projects.ROOT)))
        self.assertTrue(row['result_path'].startswith(str(projects.ROOT)))

    def test_abandoned_jobs_leave_no_row(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        self.assertIsNotNone(jobs_db.get(job['id']))
        web.abandon(job)
        self.assertIsNone(jobs_db.get(job['id']))

    def test_unknown_keeps_the_holds_in_the_row(self):
        test_jobs.StubComfy.mode = 'drop'                 # the submit answer never arrives
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'unknown')
        row = jobs_db.get(job['id'])
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['holds'], [list(h) for h in job['holds']])  # holds round-trip exactly
        self.assertTrue(any(h[0] == 'scene' for h in row['holds']))      # the scene stays claimed
        self.assertIsNone(row['workdir'])

    def test_queue_board_shows_durable_verdicts_after_a_restart(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'finished')

        rows = web.queue_view()['jobs']                   # still in memory: exactly one row
        mem = [r for r in rows if r['id'] == job['id']]
        self.assertEqual(len(mem), 1)
        self.assertNotIn('persisted', mem[0])

        web.JOBS.clear()                                  # "the server restarted"
        rows = web.queue_view()['jobs']
        persisted = [r for r in rows if r['id'] == job['id']]
        self.assertEqual(len(persisted), 1)               # not duplicated with the memory view
        row = persisted[0]
        self.assertTrue(row['persisted'])
        self.assertFalse(row['live'])
        self.assertEqual(row['state'], 'finished')
        self.assertEqual(row['kind'], 'render-draft')
        self.assertEqual(row['project'], self.slug)
        self.assertEqual(row['scene'], self.ids[0])
        self.assertEqual(row['project_name'], 'Jobsdb film')
        self.assertEqual(row['scene_title'], 'One')
        self.assertEqual(row['prompt_ids'], job['prompt_ids'])
        self.assertEqual(row['take'], (job.get('take') or {}).get('run'))
        self.assertEqual(row['error'], None)
        self.assertTrue(row['created'] and row['started'] and row['ended'])
        self.assertIsNotNone(row['queued_for'])
        self.assertIsNotNone(row['running_for'])
        # queue.html reads exactly these keys; the durable row adds only `persisted`
        self.assertEqual(set(mem[0]) | {'persisted'}, set(row))

    def test_queue_board_survives_a_broken_store(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        web.JOBS.clear()
        saved, jobs_db.FILE = jobs_db.FILE, self.root     # a directory: every read refuses
        try:
            self.assertEqual([r['id'] for r in web.queue_view()['jobs']], [])
        finally:
            jobs_db.FILE = saved


if __name__ == '__main__':
    unittest.main()
