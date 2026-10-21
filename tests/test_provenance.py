"""Generation records: every project render/enhance job leaves an immutable request snapshot plus a
sibling result file; inputs are content-addressed in refs-store; takes carry a job-backed id.

    python3 -m unittest discover tests      (from the repo root)

ComfyUI is a local stub (as in tests/test_jobs.py) that also answers /upload/image. Real ffmpeg
extracts the continuation frame. FreeVideo itself is never involved.
"""
import hashlib, json, shutil, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # stub_comfy lives in tests/

import enhance
import jobs_db
import project as projects
import stats
import web
import stub_comfy                                  # canonical /object_info map


class StubComfy(BaseHTTPRequestHandler):
    mode = 'complete'        # complete | hang
    submits = 0

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/upload/image':
            return self._json(200, {'name': f'upload-{StubComfy.submits}.png', 'subfolder': 'director'})
        if self.path == '/prompt':
            StubComfy.submits += 1
            sent = json.loads(raw or b'{}').get('prompt_id')   # echo it, like real ComfyUI
            return self._json(200, {'prompt_id': sent or f'stub-{StubComfy.submits}'})
        self._json(200, {})

    def do_GET(self):
        if self.path.startswith('/object_info'):
            info = stub_comfy.object_info()
            if self.path == '/object_info':
                return self._json(200, info)
            cls = self.path[len('/object_info/'):]
            return self._json(200, {cls: info[cls]}) if cls in info else self._json(404, {})
        if self.path.startswith('/system_stats'):
            return self._json(200, {'system': {}})
        if self.path.startswith('/history/'):
            pid = self.path.rsplit('/', 1)[-1]
            if StubComfy.mode == 'hang':
                return self._json(200, {})     # never finishes
            outputs = {'9': {'gifs': [{'filename': 'take.mp4', 'subfolder': 'director', 'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': outputs}})
        self._json(200, {})


PLAN = {'title': 'Provenance test', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'}]}


def write_npy(path, frames, height, width):
    """A minimal uint8 .npy (v1 header, 64-byte aligned) as FreeVideo leaves rgb.npy."""
    d = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({frames}, {height}, {width}, 3), }}\n"
    d += '\n' * ((-10 - len(d)) % 64)
    path.write_bytes(b'\x93NUMPY\x01\x00' + len(d).to_bytes(2, 'little') + d.encode('latin1')
                     + b'\x11' * (frames * height * width * 3))


class Provenance(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-prov-'))
        StubComfy.mode, StubComfy.submits = 'complete', 0
        self.saved = (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
                      web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS)
        projects.ROOT = self.root
        stats.FILE = self.root / 'stats.json'
        jobs_db.FILE = self.root / 'jobs.db'
        enhance.COMFY = web.COMFY = self.stub_url
        enhance.COMFY_ROOT = self.root / 'comfy'
        web.COMFY_OUTPUT = self.root / 'comfy' / 'output'
        (enhance.COMFY_ROOT / 'input').mkdir(parents=True, exist_ok=True)   # readiness
        (enhance.COMFY_ROOT / 'output').mkdir(exist_ok=True)  # checks folders exist
        web.JOB_TIMEOUT = 120.0
        web.POLL_SECONDS = 0.05
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        self.slug = projects.create('Prov film', 'nothing', 30, '16:9',
                                    [('cat.png', b'AAA'), ('dog.jpg', b'BBB')])['slug']
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

    def wait_for(self, cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def render(self, scene_id, preset='draft', variant=None):
        kind = 'render-' + preset
        job = web.new_job(kind, web.scene_stage_keys(kind), project=self.slug, scene=scene_id)
        web.run_job(job, web.render_job, self.slug, scene_id, preset, variant)
        self.assertEqual(job['state'], 'finished', job['error'])
        return job

    def snapshot(self, job_id):
        return projects.read_job(self.slug, job_id)[0]

    def take_dir(self, name='run-9'):
        """A take dir FreeVideo 0.4.9+ leaves behind: the mp4, with the raw frames gone."""
        run = web.COMFY_OUTPUT / 'director' / name
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        return str(run)

    # ---------------------------------------------------------------- snapshots

    def test_snapshot_lands_before_the_graph_is_submitted_and_never_changes(self):
        StubComfy.mode = 'hang'
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.ids[0])
        thread = threading.Thread(target=web.run_job, args=(job, web.render_job, self.slug,
                                                            self.ids[0], 'draft'), daemon=True)
        thread.start()
        snap_file = projects.job_path(self.slug, job['id'])
        self.assertTrue(self.wait_for(lambda: snap_file.is_file()), 'snapshot never written')
        self.assertFalse(job['done'])                     # still polling: the job is not over
        self.assertEqual(StubComfy.submits, 1)            # submitted once, after the record
        self.assertFalse(projects.result_path(self.slug, job['id']).exists())
        before = snap_file.read_bytes()
        job['cancel'] = True
        thread.join(10)
        self.assertEqual(snap_file.read_bytes(), before)  # the record survived the job ending

    def test_prompt_seed_and_settings_are_recorded_as_submitted(self):
        projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('seed', 0))
        job = self.render(self.ids[0])
        snap = self.snapshot(job['id'])
        self.assertEqual(snap['seed'], 0)                 # seed 0 is 0, never a reroll
        self.assertEqual(snap['prompt'], 'a cat walks left')
        self.assertEqual(snap['mode'], 'ref2va')
        self.assertEqual(snap['kind'], 'render-draft')
        take = projects.load(self.slug)['scenes'][0]['takes'][0]
        self.assertEqual(snap['take_id'], take['id'])
        gen = [n for n in snap['graph'].values() if n['class_type'] == 'FreeVideoGenerate']
        self.assertEqual(len(gen), 1)
        self.assertEqual(gen[0]['inputs']['seed'], 0)     # the recorded graph is the submitted one
        self.assertEqual(take['seed'], 0)
        self.assertEqual(take['job'], job['id'])
        self.assertEqual(take['provenance'], 'recorded')

    def test_variant_and_finishing_settings_are_recorded(self):
        run = web.COMFY_OUTPUT / 'director'               # the chained enhance needs a take dir on disk
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        job = self.render(self.ids[0], preset='final', variant=(2, 3))
        snap = self.snapshot(job['id'])
        self.assertEqual(snap['preset'], 'final')
        self.assertEqual(snap['variant'], [2, 3])
        self.assertEqual(snap['finishing'], dict(enhance.DEFAULTS, fps=enhance.FPS))
        result = projects.read_job(self.slug, job['id'])[1]
        self.assertIn('/take.mp4', result['final'])

    def test_reference_pictures_keep_order_hash_and_recreatable_copy(self):
        job = self.render(self.ids[0])
        refs = self.snapshot(job['id'])['refs']
        self.assertEqual([r['name'] for r in refs], ['picture-1.png', 'picture-2.jpg'])
        self.assertEqual([r['index'] for r in refs], [0, 1])
        self.assertEqual(refs[0]['sha256'], hashlib.sha256(b'AAA').hexdigest())
        self.assertEqual(refs[1]['sha256'], hashlib.sha256(b'BBB').hexdigest())
        self.assertEqual(projects.path(self.slug, refs[0]['store']).read_bytes(), b'AAA')
        self.assertEqual(projects.path(self.slug, refs[1]['store']).read_bytes(), b'BBB')
        self.render(self.ids[0])                          # a second render shares the store
        self.assertEqual(len(list((projects.path(self.slug, 'refs-store')).iterdir())), 2)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg needed for the continuation frame')
    def test_continuation_records_the_source_take_and_the_frame_bytes(self):
        self.render(self.ids[0])                          # stub take dir: COMFY_OUTPUT/director
        take0 = projects.load(self.slug)['scenes'][0]['takes'][0]
        npy = web.COMFY_OUTPUT / 'director' / 'video.artifacts' / 'rgb.npy'
        npy.parent.mkdir(parents=True, exist_ok=True)
        write_npy(npy, 1, 2, 2)
        job1 = self.render(self.ids[1])
        snap = self.snapshot(job1['id'])
        first = snap['first']
        png = projects.path(self.slug, 'frames', 'director-last.png')
        self.assertEqual(first['sha256'], hashlib.sha256(png.read_bytes()).hexdigest())
        self.assertEqual(first['source'], {'scene': self.ids[0], 'take': take0['id'], 'run': take0['run']})
        self.assertEqual(snap['mode'], 'i2va')
        self.assertTrue(snap['prompt'].startswith('For the target video'))
        self.assertEqual(snap['refs'], [])
        self.assertEqual(projects.path(self.slug, first['store']).read_bytes(), png.read_bytes())

    def test_take_endpoint_returns_snapshot_and_result(self):
        job = self.render(self.ids[0])
        take = projects.load(self.slug)['scenes'][0]['takes'][0]
        by_id = web.take_view(self.slug, take['id'])
        self.assertEqual(by_id['provenance'], 'recorded')
        self.assertEqual(by_id['scene'], self.ids[0])
        self.assertEqual(by_id['snapshot']['id'], job['id'])
        self.assertEqual(by_id['snapshot']['prompt'], 'a cat walks left')
        self.assertIn('FreeVideoGenerate', [n['class_type'] for n in by_id['snapshot']['graph'].values()])
        self.assertEqual(by_id['result']['run'], take['run'])
        self.assertEqual(by_id['result']['take_id'], take['id'])
        self.assertEqual(by_id['result']['path'], take['path'])
        self.assertEqual(web.take_view(self.slug, take['run']), by_id)   # addressable by run too
        with self.assertRaises(projects.ProjectError):
            web.take_view(self.slug, 'nope')

    def test_take_url_is_routed_through_the_api(self):
        job = self.render(self.ids[0])
        take = projects.load(self.slug)['scenes'][0]['takes'][0]
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)   # the real handler, spare port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{srv.server_address[1]}'
        try:
            with urllib.request.urlopen(f'{base}/api/p/{self.slug}') as r:
                view = json.loads(r.read())
            self.assertEqual(view['scenes'][0]['takes'][0]['provenance'], 'recorded')
            with urllib.request.urlopen(f'{base}/api/p/{self.slug}/take?id={take["id"]}') as r:
                tv = json.loads(r.read())
            self.assertEqual(tv['snapshot']['id'], job['id'])
            self.assertEqual(tv['result']['take_id'], take['id'])
            with self.assertRaises(urllib.error.HTTPError) as bad:
                urllib.request.urlopen(f'{base}/api/p/{self.slug}/take?id=nope')
            self.assertEqual(bad.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_project_view_exposes_provenance(self):
        self.render(self.ids[0])
        view = web.project_view(self.slug)
        self.assertEqual([t['provenance'] for t in view['scenes'][0]['takes']], ['recorded'])

    # ---------------------------------------------------------------- enhance records

    def test_enhance_records_finishing_settings_against_the_source_take(self):
        run = self.take_dir('run-9')
        take_id = projects.new_take_id()

        def add(p):
            p['scenes'][0]['takes'].append({'id': take_id, 'job': 'ancient', 'provenance': 'recorded',
                                            'run': run, 'path': run + '/video.mp4', 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, add)
        job = web.new_job('enhance', web.scene_stage_keys('enhance'), project=self.slug, scene=self.ids[0])
        web.run_job(job, web.enhance_job, run)
        self.assertEqual(job['state'], 'finished', job['error'])
        snap, result = projects.read_job(self.slug, job['id'])
        self.assertEqual(snap['kind'], 'enhance')
        self.assertEqual(snap['id'], job['id'])
        self.assertEqual(snap['source'], {'take': take_id, 'run': run, 'provenance': 'recorded'})
        self.assertEqual(snap['finishing'], dict(enhance.DEFAULTS, fps=enhance.FPS))
        self.assertIn('/take.mp4', result['final'])

    def test_enhance_of_a_legacy_take_says_legacy(self):
        run = self.take_dir('run-10')

        def add(p):
            p['scenes'][0]['takes'].append({'run': run, 'path': run + '/video.mp4', 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, add)
        job = web.new_job('enhance', web.scene_stage_keys('enhance'), project=self.slug, scene=self.ids[0])
        web.run_job(job, web.enhance_job, run)
        snap = self.snapshot(job['id'])
        self.assertEqual(snap['source'], {'take': None, 'run': run, 'provenance': 'legacy'})

    # ---------------------------------------------------------------- legacy takes

    def test_legacy_takes_stay_legacy_and_are_never_backfilled(self):
        run = self.take_dir('run-11')

        def add(p):
            p['scenes'][0]['takes'].append({'run': run, 'path': run + '/video.mp4', 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, add)
        take = projects.load(self.slug)['scenes'][0]['takes'][0]     # reloaded from disk
        self.assertNotIn('id', take)                                 # nothing invented on load
        self.assertNotIn('provenance', take)
        self.assertEqual(projects.take_provenance(take), 'legacy')
        self.assertEqual(web.project_view(self.slug)['scenes'][0]['takes'][0]['provenance'], 'legacy')
        view = web.take_view(self.slug, run)                         # addressed by run
        self.assertEqual(view['provenance'], 'legacy')
        self.assertEqual(view['take']['run'], run)
        self.assertNotIn('snapshot', view)

    # ---------------------------------------------------------------- records on disk

    def test_snapshot_and_refs_store_survive_pruning(self):
        job = self.render(self.ids[0])
        star_run = projects.load(self.slug)['scenes'][0]['takes'][0]['run']
        loose = self.take_dir('run-12')

        def add(p):
            p['scenes'][0]['star'] = star_run
            p['scenes'][0]['takes'].append({'run': loose, 'path': loose + '/video.mp4', 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, add)
        out = {}
        projects.mutate(self.slug, lambda p: out.update(projects.prune_apply(p)))
        self.assertEqual(out['moved'], 1)                          # the loose take moved to trash
        self.assertFalse(Path(loose).exists())
        self.assertTrue(projects.job_path(self.slug, job['id']).is_file())
        self.assertTrue(projects.result_path(self.slug, job['id']).is_file())
        self.assertEqual(len(list((projects.path(self.slug, 'refs-store')).iterdir())), 2)
        self.assertEqual(self.snapshot(job['id'])['prompt'], 'a cat walks left')

    def test_prune_never_offers_record_paths_as_candidates(self):
        run = self.take_dir('run-13')
        record_run = str(projects.path(self.slug, 'refs-store'))     # a run pointing at project records

        def add(p):
            p['scenes'][0]['star'] = run
            p['scenes'][0]['takes'].append({'run': run, 'path': run + '/video.mp4', 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
            p['scenes'][0]['takes'].append({'run': record_run, 'path': None, 'url': None,
                                            'preset': 'draft', 'seed': 1, 'created': 0.0,
                                            'final': None, 'final_url': None, 'variant': None})
        projects.mutate(self.slug, add)
        plan = projects.prune_plan(projects.load(self.slug))
        self.assertEqual([c['run'] for c in plan['candidates']], [])
        self.assertEqual(plan['skipped'][0]['reason'], 'kept: generation record')

    def test_record_files_are_write_once(self):
        projects.write_job_snapshot(self.slug, {'id': 'abcd1234ef56', 'kind': 'render-draft'})
        with self.assertRaises(projects.ProjectError):
            projects.write_job_snapshot(self.slug, {'id': 'abcd1234ef56', 'kind': 'render-draft'})
        projects.write_job_result(self.slug, 'abcd1234ef56', {'job': 'abcd1234ef56'})
        with self.assertRaises(projects.ProjectError):
            projects.write_job_result(self.slug, 'abcd1234ef56', {'job': 'abcd1234ef56'})
        snap, result = projects.read_job(self.slug, 'abcd1234ef56')
        self.assertEqual(snap['kind'], 'render-draft')
        self.assertEqual(result['job'], 'abcd1234ef56')
        with self.assertRaises(projects.ProjectError):
            projects.job_path(self.slug, '../../escape')


if __name__ == '__main__':
    unittest.main()
