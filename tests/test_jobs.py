"""Job lifecycle: every job reaches a terminal state, scenes/takes are reserved while a job
runs, doomed work is refused before acceptance, and an unanswered submit is never retried.

    python3 -m unittest discover tests      (from the repo root)

ComfyUI is a local stub: it answers /prompt, /history, /free and can be told to hang, drop
the connection or reject the graph. FreeVideo itself is never involved.
"""
import json, shutil, sys, tempfile, threading, time, unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enhance
import jobs_db
import project as projects
import stats
import web


class StubComfy(BaseHTTPRequestHandler):
    mode = 'complete'      # complete | hang | drop | reject | reject_prompt_id | mismatch | gate (POST /prompt stalls) | gone | interrupted | failed
    submits = 0
    pending, running_ids = [], []        # the stub's /queue model
    deletes, interrupts = [], 0
    bodies, prompt_ids = [], []          # every POST /prompt body, and the ids the stub handed back
    honour_delete = True                 # False: /queue delete is ignored (cancel cannot be confirmed)
    at_prompt, gate = threading.Event(), threading.Event()

    @classmethod
    def reset(cls):
        cls.pending, cls.running_ids = [], []
        cls.deletes, cls.interrupts, cls.honour_delete = [], 0, True
        cls.bodies, cls.prompt_ids = [], []
        cls.at_prompt, cls.gate = threading.Event(), threading.Event()

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _queue(self):
        row = lambda pos, pid: [pos, pid, [], {}, '']
        return {'queue_running': [row(0, p) for p in StubComfy.running_ids],
                'queue_pending': [row(n + 1, p) for n, p in enumerate(StubComfy.pending)]}

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/prompt':
            StubComfy.at_prompt.set()
            body = json.loads(raw or b'{}')
            StubComfy.bodies.append(body)
            if StubComfy.mode == 'reject_prompt_id' and 'prompt_id' in body:
                return self._json(400, {'error': {'type': 'invalid_prompt_id',
                                                  'message': 'unexpected prompt_id field'}})
            if StubComfy.mode == 'gate':
                StubComfy.gate.wait(10)        # the cancel may land while this POST is in flight
            StubComfy.submits += 1
            if StubComfy.mode == 'reject':
                return self._json(400, {'error': 'bad graph'})
            if StubComfy.mode == 'drop':
                self.connection.close()        # answer nothing: the submit stays ambiguous
                return
            if StubComfy.mode == 'mismatch':
                pid = f'server-{StubComfy.submits}'    # answers an id it made up, not ours
            else:
                pid = body.get('prompt_id') or f'stub-{StubComfy.submits}'   # echo: like real ComfyUI
            StubComfy.prompt_ids.append(pid)
            if StubComfy.mode == 'gate':
                StubComfy.pending.append(pid)
            elif StubComfy.mode == 'hang':
                StubComfy.running_ids.append(pid)   # hangs mid-execution: /history stays empty
            return self._json(200, {'prompt_id': pid})
        if self.path == '/queue':              # {"delete": [ids]}: drop them from the queue
            ids = set(json.loads(raw or b'{}').get('delete', []))
            StubComfy.deletes.append(sorted(ids))
            if StubComfy.honour_delete:
                StubComfy.pending = [p for p in StubComfy.pending if p not in ids]
                StubComfy.running_ids = [p for p in StubComfy.running_ids if p not in ids]
            return self._json(200, self._queue())
        if self.path == '/interrupt':          # stops whatever the stub is "executing"
            StubComfy.interrupts += 1
            StubComfy.running_ids = []
            return self._json(200, {})
        self._json(200, {})

    def do_GET(self):
        if self.path == '/queue':
            return self._json(200, self._queue())
        if self.path.startswith('/history/'):
            pid = self.path.rsplit('/', 1)[-1]
            if StubComfy.mode in ('hang', 'gate', 'gone'):
                return self._json(200, {})     # never finishes, or leaves no record at all
            if StubComfy.mode == 'interrupted':
                return self._json(200, {pid: {'status': {'completed': False, 'interrupted': True,
                                                         'messages': [['execution_interrupted',
                                                                       {'exception_message':
                                                                        'prompt interrupted by user'}]]}}})
            if StubComfy.mode == 'failed':
                return self._json(200, {pid: {'status_str': 'error', 'status':
                                              {'completed': False,
                                               'messages': [['execution_error',
                                                             {'exception_message': 'CUDA out of memory'}]]}}})
            outputs = {'9': {'gifs': [{'filename': 'take.mp4', 'subfolder': 'director', 'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': outputs}})
        self._json(200, {})


PLAN = {'title': 'Job test', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'}]}


class Jobs(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-jobs-'))
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
        self.slug = projects.create('Jobs film', 'nothing', 30, '16:9', [])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]
        def give_prompts(p):
            for s in p['scenes']:
                s['prompt'] = 'a cat walks left'
        projects.mutate(self.slug, give_prompts)

    def tearDown(self):
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def take_dir(self, name='run-7'):
        """A take dir FreeVideo 0.4.9+ leaves behind: the mp4, with the raw frames gone."""
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

    # ---------------------------------------------------------------- states

    def test_every_job_reaches_a_terminal_state(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, 'missing-scene', 'draft')
        self.assertEqual(job['state'], 'failed')
        self.assertTrue(job['done'])
        self.assertEqual(job['error'], 'unknown scene')
        self.assertEqual(web.RESERVES, {})

    def test_illegal_transitions_are_programmer_errors(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        with self.assertRaises(AssertionError):
            web.transition(job, 'running')                      # queued -> running skips preparing
        web.transition(job, 'preparing')
        web.transition(job, 'submitted')
        web.transition(job, 'running')
        web.finish(job)
        with self.assertRaises(AssertionError):
            web.transition(job, 'running')                      # terminal states are locked

    def test_public_exposes_state(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        self.assertEqual(web.public(job)['state'], 'queued')
        self.assertIs(web.public(job)['done'], False)

    # ---------------------------------------------------------------- cancel

    def test_cancel_before_start_never_submits(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        job['cancel'] = True
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(job['error'], 'cancelled')
        self.assertEqual(StubComfy.submits, 0)
        self.assertEqual(web.RESERVES, {})

    def test_cancel_mid_prep_never_submits(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        ready, release = threading.Barrier(2), threading.Event()
        def sleepy(j):
            ready.wait(5)
            release.wait(5)          # the cancel lands while we are still preparing
            web.check_cancel(j)
        t = threading.Thread(target=web.run_job, args=(job, sleepy))
        t.start()
        ready.wait(5)
        job['cancel'] = True
        release.set()
        t.join(10)
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(StubComfy.submits, 0)

    def test_cancel_mid_poll_does_not_resubmit(self):
        StubComfy.mode = 'hang'
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        t = threading.Thread(target=web.run_job,
                             args=(job, web.render_job, self.slug, self.ids[0], 'draft'))
        t.start()
        self.assertTrue(self.wait_for(lambda: StubComfy.submits == 1))
        job['cancel'] = True
        t.join(30)
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(StubComfy.submits, 1)
        self.assertEqual(web.RESERVES, {})

    def test_cancel_during_submit_targets_only_the_new_prompt(self):
        StubComfy.mode = 'gate'
        StubComfy.running_ids = ['someone-else']        # an unrelated prompt occupies ComfyUI
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        t = threading.Thread(target=web.run_job,
                             args=(job, web.render_job, self.slug, self.ids[0], 'draft'))
        t.start()
        StubComfy.at_prompt.wait(5)                     # the POST /prompt is in flight
        job['cancel'] = True                            # the cancel lands mid-submit
        StubComfy.gate.set()
        t.join(15)
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(job['prompt_ids'], StubComfy.prompt_ids)
        self.assertEqual(StubComfy.deletes, [sorted(job['prompt_ids'])])   # exactly our prompt is dropped
        self.assertEqual(StubComfy.interrupts, 0)           # none of ours was executing: never interrupt
        self.assertEqual(StubComfy.running_ids, ['someone-else'])   # the unrelated prompt is untouched

    def test_unconfirmed_cancel_becomes_unknown(self):
        StubComfy.mode = 'gate'
        StubComfy.honour_delete = False                 # ComfyUI ignores our delete: did it stop?
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        t = threading.Thread(target=web.run_job,
                             args=(job, web.render_job, self.slug, self.ids[0], 'draft'))
        t.start()
        StubComfy.at_prompt.wait(5)
        job['cancel'] = True
        StubComfy.gate.set()
        t.join(15)
        self.assertEqual(job['state'], 'unknown')
        self.assertIn('may still be executing', job['error'])
        self.assertEqual(StubComfy.interrupts, 0)
        self.assertEqual(StubComfy.submits, 1)          # and it is never resubmitted

    def test_poll_cancel_interrupts_when_our_prompt_is_running(self):
        StubComfy.mode = 'hang'                         # our prompt is the one executing
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        t = threading.Thread(target=web.run_job,
                             args=(job, web.render_job, self.slug, self.ids[0], 'draft'))
        t.start()
        self.assertTrue(self.wait_for(lambda: StubComfy.submits == 1))
        job['cancel'] = True
        t.join(15)
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(StubComfy.interrupts, 1)       # ours was executing -> interrupt is correct
        self.assertEqual(StubComfy.submits, 1)


    def test_cancel_project_flags_only_that_films_jobs(self):
        other = projects.create('Other film', 'nothing else', 30, '16:9', [])['slug']
        mine_a = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug)
        mine_b = web.new_job('render-final', ['render'], project=self.slug, scene=self.ids[1])
        theirs = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=other)
        web.cancel_project(self.slug)
        self.assertTrue(mine_a['cancel'] and mine_b['cancel'])
        self.assertFalse(theirs['cancel'])

    # ---------------------------------------------------------------- reservations

    def test_second_render_of_a_scene_is_refused(self):
        first = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        second = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.reserve(first, [web.scene_key(self.slug, self.ids[0])])
        with self.assertRaises(web.Busy) as caught:
            web.reserve(second, [web.scene_key(self.slug, self.ids[0])])
        self.assertEqual(caught.exception.args[0], f'busy: scene {self.ids[0]} already has a running job')
        self.assertEqual(web.SCENE_JOB[(self.slug, self.ids[0])], first['id'])
        web.release(first)
        web.reserve(second, [web.scene_key(self.slug, self.ids[0])])
        web.release(second)

    def test_reservation_race_admits_exactly_one(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 2.0                            # the holder keeps the scene until it times out
        first = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        t = threading.Thread(target=web.run_job,
                             args=(first, web.render_job, self.slug, self.ids[0], 'draft'))
        t.start()
        self.assertTrue(self.wait_for(lambda: StubComfy.submits == 1))
        second = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(second, web.render_job, self.slug, self.ids[0], 'draft')   # refused at the reservation
        self.assertEqual(second['state'], 'failed')
        self.assertTrue(second['error'].startswith('busy:'))
        t.join(30)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(StubComfy.submits, 1)
        self.assertEqual(web.RESERVES, {web.scene_key(self.slug, self.ids[0]): first['id']})   # unknown: still held

    def test_save_must_keep_reserved_scenes(self):
        held = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[1])
        web.reserve(held, [web.scene_key(self.slug, self.ids[1])])
        kept = [{'id': self.ids[0]}, {'id': self.ids[1], 'prompt': 'x'}]
        web.check_keeps_reserved(self.slug, {'scenes': kept})          # keeping every scene: fine
        with self.assertRaises(web.Busy) as caught:
            web.check_keeps_reserved(self.slug, {'scenes': [{'id': self.ids[0]}]})
        self.assertIn(f'scene {self.ids[1]} has a running job', caught.exception.args[0])
        web.transition(held, 'preparing')
        web.finish(held)
        web.check_keeps_reserved(self.slug, {'scenes': [{'id': self.ids[0]}]})   # released: deletion is fine

    def test_start_batch_refuses_while_a_job_lives(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug)
        with self.assertRaises(web.Busy) as caught:
            web.start_batch(self.slug, 'Drafting scenes', 0, lambda state: None)
        self.assertTrue(caught.exception.args[0].startswith('busy:'))
        self.assertEqual(web.BATCH, {})
        web.abandon(job)

    def test_duplicate_enhance_returns_the_live_job(self):
        run = self.take_dir('run-1')
        first = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug)
        self.assertIsNone(web.reserve(first, [web.take_key(run)]))
        dup = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug)
        self.assertIs(web.reserve(dup, [web.take_key(run)], reuse=True), first)
        web.transition(first, 'preparing')
        web.finish(first)                                  # terminal: a fresh enhance may start
        self.assertIsNone(web.reserve(dup, [web.take_key(run)], reuse=True))

    # ---------------------------------------------------------------- pre-acceptance validation

    def test_missing_reference_picture_is_refused(self):
        projects.mutate(self.slug, lambda p: p.__setitem__('refs', ['cat.png']))
        with self.assertRaises(projects.ProjectError) as caught:
            web.validate_scene_media(projects.load(self.slug), 0)
        self.assertIn('reference picture missing: cat.png', caught.exception.args[0])

    def test_continue_without_previous_take_dir_is_refused(self):
        def star_missing_take(p):
            s = p['scenes'][0]
            s['takes'].append({'run': 'director/gone', 'path': 'x', 'url': None, 'preset': 'draft',
                               'seed': 1, 'created': 0.0, 'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, star_missing_take)
        with self.assertRaises(FileNotFoundError):
            web.validate_scene_media(projects.load(self.slug), 1)   # scene 2 continues scene 1

    def test_happy_render_finishes_and_records_the_take(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'finished')
        self.assertIsNone(job['error'])
        self.assertEqual(StubComfy.submits, 1)
        self.assertEqual(len(job['prompt_ids']), 1)
        scene = projects.load(self.slug)['scenes'][0]
        self.assertEqual(len(scene['takes']), 1)
        self.assertTrue(str(scene['takes'][0]['run']).endswith('/director'))
        self.assertEqual(web.RESERVES, {})

    # ---------------------------------------------------------------- submit and timeout

    def test_rejected_graph_fails_not_unknown(self):
        StubComfy.mode = 'reject'
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'failed')
        self.assertIn('ComfyUI rejected the job', job['error'])
        self.assertEqual(StubComfy.submits, 1)

    def test_dropped_submit_is_unknown_and_never_resubmitted(self):
        StubComfy.mode = 'drop'
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'unknown')
        self.assertEqual(StubComfy.submits, 1)                 # exactly one POST /prompt, no retry
        self.assertIn('may still be executing', job['error'])
        self.assertEqual(web.RESERVES, {web.scene_key(self.slug, self.ids[0]): job['id']})   # the submit may have landed: keep holding

    def test_timeout_keeps_the_prompt_id_and_says_unknown(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'unknown')
        self.assertEqual(len(job['prompt_ids']), 1)
        self.assertIn('may still be executing', job['error'])
        self.assertEqual(web.RESERVES, {web.scene_key(self.slug, self.ids[0]): job['id']})   # unknown: the scene stays held

    # ---------------------------------------------------------------- unknown jobs keep the scene

    def test_unknown_job_keeps_the_scene_reserved(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        key = web.scene_key(self.slug, self.ids[0])
        self.assertEqual(job['state'], 'unknown')
        self.assertEqual(web.RESERVES.get(key), job['id'])      # the take may still land in this scene
        self.assertEqual(job['holds'], [key])
        self.assertEqual(web.reserved_scenes(self.slug), {self.ids[0]})
        second = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        with self.assertRaises(web.Busy) as caught:
            web.reserve(second, [key])
        self.assertEqual(caught.exception.args[0],
                         f'busy: job {job["id"]} state unknown - ComfyUI prompt {job["prompt_ids"][0]} may still be executing')
        dup = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        with self.assertRaises(web.Busy):                       # reuse is off the table: the holder is dead to us
            web.reserve(dup, [key], reuse=True)

    def test_reconcile_proves_the_unknown_job_finished_and_records_the_take_once(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(web.RESERVES.get(web.scene_key(self.slug, self.ids[0])), job['id'])
        StubComfy.mode = 'complete'                            # the prompt did finish after all
        StubComfy.running_ids = []
        self.assertEqual(web.reconcile(job), 'finished')
        self.assertEqual(job['resolved']['verdict'], 'finished')
        self.assertEqual(web.RESERVES, {})                     # the scene is free again
        self.assertEqual(len(projects.load(self.slug)['scenes'][0]['takes']), 1)
        self.assertEqual(web.reconcile(job), 'finished')       # asking twice changes nothing
        self.assertEqual(len(projects.load(self.slug)['scenes'][0]['takes']), 1)
        fresh = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(fresh, web.render_job, self.slug, self.ids[0], 'draft')   # new work is accepted again
        self.assertEqual(fresh['state'], 'finished')

    def test_reconcile_while_still_running_keeps_everything(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(web.reconcile(job), 'running')        # the prompt is still executing
        self.assertIsNone(job.get('resolved'))
        self.assertEqual(web.RESERVES.get(web.scene_key(self.slug, self.ids[0])), job['id'])
        self.assertEqual(web.reserved_scenes(self.slug), {self.ids[0]})

    def test_reconcile_releases_a_prompt_that_stopped_or_failed(self):
        web.JOB_TIMEOUT = 0.4
        for n, (mode, verdict) in enumerate((('interrupted', 'interrupted'),
                                             ('failed', 'failed'), ('gone', 'lost'))):
            with self.subTest(mode=mode):
                sid = self.ids[n % len(self.ids)]
                StubComfy.mode = 'hang'
                job = web.new_job('render-draft', ['render'], project=self.slug, scene=sid)
                web.run_job(job, web.render_job, self.slug, sid, 'draft')
                StubComfy.running_ids = []                     # the prompt finally left the queue
                StubComfy.mode = mode                          # the history says what happened to it
                self.assertEqual(web.reconcile(job), verdict)
                self.assertEqual(job['resolved']['verdict'], verdict)
                self.assertIsNone(web.RESERVES.get(web.scene_key(self.slug, sid)))
                self.assertEqual(web.reconcile(job), verdict)  # idempotent: nothing further changes

    def test_queue_view_shows_an_unknown_job_with_its_prompts(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        row = next(r for r in web.queue_view()['jobs'] if r['id'] == job['id'])
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['prompt_ids'], job['prompt_ids'])   # our submitted id survives in the row
        self.assertIn('may still be executing', row['error'])
        self.assertIsNone(row['resolved'])

    def test_dismissing_an_unknown_job_frees_the_scene(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{srv.server_address[1]}/api/job/{job["id"]}/resolve',
                data=b'{}', method='POST', headers={'Content-Type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=10).read())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(r['proved'], 'running')               # nothing provable: our prompt is still queued
        self.assertEqual(job['resolved']['by'], 'user')
        self.assertIn('duplicate take', r['warning'])
        self.assertEqual(job['state'], 'unknown')              # the state itself is never rewritten

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

    def test_reconcile_route_proves_the_truth_without_dismissing(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'unknown')
        StubComfy.mode = 'complete'
        StubComfy.running_ids = []
        r = self.post(f'/api/job/{job["id"]}/reconcile')
        self.assertEqual(r['proved'], 'finished')
        self.assertEqual(job['resolved']['by'], 'reconcile')
        key = web.scene_key(self.slug, str(self.ids[0]))
        self.assertNotIn(key, web.RESERVES)                    # a proven verdict releases the holds
        self.assertEqual(job['state'], 'unknown')              # ... but never rewrites the state
        self.assertNotIn(key, web.SCENE_JOB)                   # the scene is free for new work

    def test_reconcile_route_reports_still_running_and_keeps_the_holds(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        r = self.post(f'/api/job/{job["id"]}/reconcile')
        self.assertEqual(r['proved'], 'running')                # ComfyUI still holds the prompt
        self.assertIsNone(job.get('resolved'))                  # nothing dismissed, nothing released
        self.assertIn(web.scene_key(self.slug, str(self.ids[0])), web.RESERVES)

    def test_reconcile_route_refuses_jobs_it_cannot_answer(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post('/api/job/nope000000000000/reconcile')
        self.assertEqual(e.exception.code, 404)
        run = self.take_dir('run-r')                            # a finished job is not "unknown"
        job = web.new_job('enhance', ['upscale'], project=self.slug, scene=self.ids[0])
        web.run_job(job, lambda *a: None)
        self.assertEqual(job['state'], 'finished')
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(f'/api/job/{job["id"]}/reconcile')
        self.assertEqual(e.exception.code, 400)

    # ---------------------------------------------------------------- queue board

    def test_started_and_ended_are_recorded_once_at_the_right_transition(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug, scene=self.ids[0])
        self.assertIsNotNone(job['created'])
        self.assertIsNone(job.get('started'))
        web.transition(job, 'preparing')
        self.assertIsNone(job.get('started'))                 # preparing is still our queue
        time.sleep(0.2)
        web.transition(job, 'submitted')
        started = job['started']
        self.assertIsNotNone(started)                         # first moment ComfyUI verifiably had it
        web.transition(job, 'running')
        self.assertEqual(job['started'], started)             # never moved again
        self.assertIsNone(job.get('ended'))
        web.finish(job)
        self.assertIsNotNone(job['ended'])

    def test_queue_board_lists_a_recent_finished_job_with_both_times(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug, scene=self.ids[0])
        web.reserve(job, [web.scene_key(self.slug, self.ids[0])])
        web.transition(job, 'preparing')
        time.sleep(0.2)
        web.transition(job, 'submitted')
        web.transition(job, 'running')
        time.sleep(0.2)
        web.finish(job)
        row = next(r for r in web.queue_view()['jobs'] if r['id'] == job['id'])
        self.assertEqual(row['state'], 'finished')
        self.assertFalse(row['live'])
        self.assertGreaterEqual(row['queued_for'], 0)
        self.assertGreaterEqual(row['running_for'], 0)        # separate from the queued time
        for f in ('created', 'started', 'ended'):
            self.assertIsInstance(row[f], float)
        job['ended'] = time.time() - web.RECENT_DONE_SECONDS - 5
        web.db_note(job)                                    # the durable row ages along with it
        self.assertNotIn(job['id'], [r['id'] for r in web.queue_view()['jobs']])   # old verdicts leave

    def test_cancel_route_cancels_a_preparing_job_that_never_reached_comfyui(self):
        job = web.new_job('enhance', ['upscale'], project=self.slug, scene=self.ids[0])
        web.reserve(job, [web.scene_key(self.slug, self.ids[0])])
        web.transition(job, 'preparing')
        r = self.post(f'/api/job/{job["id"]}/cancel')
        self.assertTrue(r['proved'])
        self.assertEqual(job['state'], 'cancelled')
        self.assertIsNotNone(job['ended'])                    # it never ran, so no running time
        self.assertIsNone(job.get('started'))
        row = next(x for x in web.queue_view()['jobs'] if x['id'] == job['id'])
        self.assertEqual((row['state'], row['running_for']), ('cancelled', None))
        self.assertIsNone(web.reserve(web.new_job('enhance', ['upscale']),
                                      [web.scene_key(self.slug, self.ids[0])]))   # scene free again

    def test_cancel_route_flags_a_running_job_and_asks_comfyui_to_stop(self):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.reserve(job, [web.scene_key(self.slug, self.ids[0])])
        web.transition(job, 'preparing')
        job['prompt_ids'] = ['stub-1', 'stub-2']
        StubComfy.running_ids = ['stub-1']                    # one prompt executes,
        StubComfy.pending = ['stub-2']                        # the other waits in the queue
        web.transition(job, 'submitted')
        web.transition(job, 'running')
        r = self.post(f'/api/job/{job["id"]}/cancel')
        self.assertTrue(r['proved'])
        self.assertTrue(job['cancel'])                        # the worker finalises on its next check
        self.assertEqual(job['state'], 'running')
        self.assertEqual(StubComfy.interrupts, 1)
        self.assertEqual(StubComfy.deletes, [['stub-2']])     # the waiting one left the queue
        self.assertEqual(StubComfy.running_ids, [])           # the executing one was stopped

    def test_cancel_route_refuses_unknown_finished_and_missing_jobs(self):
        StubComfy.mode = 'hang'
        web.JOB_TIMEOUT = 0.4
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        web.run_job(job, web.render_job, self.slug, self.ids[0], 'draft')
        self.assertEqual(job['state'], 'unknown')
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(f'/api/job/{job["id"]}/cancel')
        self.assertEqual(e.exception.code, 409)               # unknown: reconcile/dismiss instead
        done = web.new_job('enhance', ['upscale'], project=self.slug, scene=self.ids[1])
        web.run_job(done, lambda *a: None)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(f'/api/job/{done["id"]}/cancel')
        self.assertEqual(e.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post('/api/job/nope000000000000/cancel')
        self.assertEqual(e.exception.code, 404)

    # ---------------------------------------------------------------- enhance

    def test_enhance_finishes_and_releases_the_take(self):
        run = self.take_dir('run-9')
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug, scene=self.ids[0])
        finals = []
        web.run_job(job, web.enhance_job, run, finals.append)
        self.assertEqual(job['state'], 'finished', job['error'])
        self.assertIn('/take.mp4', job['final']['path'])
        self.assertEqual(finals, [job['final']])
        self.assertEqual(web.RESERVES, {})

    def test_enhance_dies_cleanly_on_a_missing_take(self):
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'], project=self.slug)
        web.run_job(job, web.enhance_job, str(self.root / 'nope'))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('only FreeVideo renders can be enhanced', job['error'])
        self.assertEqual(StubComfy.submits, 0)
        self.assertEqual(web.RESERVES, {})


if __name__ == '__main__':
    unittest.main()
