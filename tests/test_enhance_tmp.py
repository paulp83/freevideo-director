"""Enhance temp isolation: each enhance run works in its own per-job temp dir, and that dir is
gone after success, failure and confirmed cancel; an unknown job keeps its dir until reconcile
or a dismissal proves the ComfyUI prompt stopped, and crash leftovers get swept after a day.

    python3 -m unittest discover tests      (from the repo root)

ffmpeg is a fake Popen and ComfyUI a local stub server; nothing touches a GPU.
"""
import io, json, os, shutil, sys, tempfile, threading, time, types, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enhance
import jobs_db
import stats
import web


class StubComfy(BaseHTTPRequestHandler):
    mode = 'complete'          # complete | reject | gate (history withheld until released)
    release = threading.Event()
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
        if self.path == '/prompt':
            StubComfy.submits += 1
            if StubComfy.mode == 'reject':
                return self._json(400, {'error': 'bad graph'})
            sent = json.loads(raw or b'{}').get('prompt_id')   # echo it, like real ComfyUI
            return self._json(200, {'prompt_id': sent or f'stub-{StubComfy.submits}'})
        self._json(200, {})

    def do_GET(self):
        if self.path.startswith('/history/'):
            if StubComfy.mode == 'gate' and not StubComfy.release.is_set():
                return self._json(200, {})             # still "running"
            pid = self.path.rsplit('/', 1)[-1]
            outputs = {'6': {'gifs': [{'filename': 'take-final-00001.mp4', 'subfolder': 'director',
                                       'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': outputs}})
        self._json(200, {})


class FakePopen:
    """Stands in for the ffmpeg pipe; rc decides whether the intermediate 'write' fails."""
    procs = []
    rc = 0

    def __init__(self, cmd, stdin=None):
        FakePopen.procs.append(cmd)
        self.stdin = io.BytesIO()

    def wait(self):
        return FakePopen.rc


class EnhanceTmp(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-enhtmp-'))
        StubComfy.mode, StubComfy.submits = 'complete', 0
        StubComfy.release.clear()
        FakePopen.procs, FakePopen.rc = [], 0
        self.saved = (enhance.COMFY, enhance.COMFY_ROOT, enhance.subprocess, enhance.time,
                      web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS, stats.FILE,
                      jobs_db.FILE)
        enhance.COMFY = web.COMFY = self.stub_url
        enhance.COMFY_ROOT = self.root / 'comfy'
        web.COMFY_OUTPUT = self.root / 'comfy' / 'output'
        enhance.subprocess = types.SimpleNamespace(Popen=FakePopen, PIPE=-1)
        enhance.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)   # free_gpu's 3s nap
        web.JOB_TIMEOUT, web.POLL_SECONDS = 120.0, 0.02
        stats.FILE = self.root / 'stats.json'
        jobs_db.FILE = self.root / 'jobs.db'
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()

    def tearDown(self):
        (enhance.COMFY, enhance.COMFY_ROOT, enhance.subprocess, enhance.time,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS, stats.FILE,
         jobs_db.FILE) = self.saved
        StubComfy.release.set()
        shutil.rmtree(self.root, ignore_errors=True)

    def take_dir(self, name, raw=False, frames=2, height=4, width=6):
        """A take dir: mp4 + video.artifacts; with raw= the rgb.npy FreeVideo <=0.4.8 keeps."""
        run = web.COMFY_OUTPUT / 'director' / name
        (run / 'video.artifacts').mkdir(parents=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        if raw:
            header = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({frames}, {height}, {width}, 3), }}\n"
            data = header.encode('latin1')
            with open(run / 'video.artifacts' / 'rgb.npy', 'wb') as f:
                f.write(b'\x93NUMPY' + bytes([1, 0]) + len(data).to_bytes(2, 'little') + data)
                f.write(b'\0' * frames * height * width * 3)
        return run

    def workdirs(self):
        parent = enhance.workdir_parent()
        return sorted(p.name for p in parent.iterdir()) if parent.is_dir() else []

    def run_enhance(self, job, run):
        t = threading.Thread(target=web.run_job, args=(job, web.enhance_job, str(run)), daemon=True)
        t.start()
        return t

    def wait_for(self, cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    # ---------------------------------------------------------------- isolation

    def test_two_enhances_get_distinct_dirs_and_both_are_removed(self):
        StubComfy.mode = 'gate'                        # both jobs stay live inside poll()
        run_a, run_b = self.take_dir('alpha'), self.take_dir('beta')
        job_a = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        job_b = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        ta, tb = self.run_enhance(job_a, run_a), self.run_enhance(job_b, run_b)
        self.assertTrue(self.wait_for(lambda: job_a['prompt_ids'] and job_b['prompt_ids']))
        dirs = self.workdirs()
        self.assertEqual(len(dirs), 2, 'concurrent enhances must not share one temp dir')
        self.assertIn(f'alpha-{job_a["id"]}-', [d for d in dirs if d.startswith('alpha-')][0])
        self.assertIn(f'beta-{job_b["id"]}-', [d for d in dirs if d.startswith('beta-')][0])
        StubComfy.release.set()
        ta.join(10); tb.join(10)
        self.assertEqual([job_a['state'], job_b['state']], ['finished', 'finished'])
        self.assertEqual(self.workdirs(), [], 'a finished enhance leaves its temp dir behind')

    def test_workdir_is_removed_after_failure(self):
        StubComfy.mode = 'reject'                      # ComfyUI refuses the graph
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        web.run_job(job, web.enhance_job, str(self.take_dir('run-f')))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('ComfyUI rejected the job', job['error'])
        self.assertEqual(self.workdirs(), [])

    def test_workdir_is_removed_when_ffmpeg_fails(self):
        run = self.take_dir('run-ff', raw=True)
        FakePopen.rc = 1
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        web.run_job(job, web.enhance_job, str(run))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('ffmpeg failed', job['error'])
        self.assertEqual(self.workdirs(), [])

    def test_workdir_is_removed_after_cancel(self):
        StubComfy.mode = 'gate'
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        t = self.run_enhance(job, self.take_dir('run-c'))
        self.assertTrue(self.wait_for(lambda: job['prompt_ids']))   # inside the polling stage
        job['cancel'] = True
        t.join(10)
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(self.workdirs(), [])

    def test_submit_cleans_its_workdir_when_the_graph_is_refused(self):
        StubComfy.mode = 'reject'                      # the enhance.py CLI path
        with self.assertRaises(Exception):
            enhance.submit(str(self.take_dir('run-cli')))
        self.assertEqual(self.workdirs(), [])

    # ---------------------------------------------------------------- unknown jobs keep the dir

    def test_unknown_enhance_keeps_its_dir_until_reconciled(self):
        StubComfy.mode = 'gate'
        web.JOB_TIMEOUT = 0.3                          # the wall-clock guard trips: state unknown
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        web.run_job(job, web.enhance_job, str(self.take_dir('run-u')))
        self.assertEqual(job['state'], 'unknown')
        dirs = self.workdirs()
        self.assertEqual(len(dirs), 1, 'an unknown enhance must keep its temp dir')
        self.assertEqual(Path(job['workdir']).name, dirs[0])
        self.assertTrue(job['holds'])
        StubComfy.release.set()                        # now ComfyUI admits the prompt completed
        self.assertEqual(web.reconcile(job), 'finished')
        self.assertEqual(web.reconcile(job), 'finished')               # idempotent
        self.assertIsNotNone(job['final'], 'the finished enhance must record its late final')
        self.assertEqual(self.workdirs(), [], 'a resolved unknown job must not keep its dir')
        self.assertIsNone(job['workdir'])
        self.assertEqual(web.RESERVES, {})

    def test_unconfirmed_cancel_keeps_the_dir(self):
        StubComfy.mode = 'gate'
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        t = self.run_enhance(job, self.take_dir('run-uc'))
        self.assertTrue(self.wait_for(lambda: job['prompt_ids']))
        web.COMFY = enhance.COMFY = 'http://127.0.0.1:1'   # ComfyUI goes dark mid-flight
        job['cancel'] = True
        t.join(10)
        self.assertEqual(job['state'], 'unknown')      # nobody can prove the prompt stopped
        self.assertEqual(len(self.workdirs()), 1)
        self.assertTrue(job['holds'])

    def test_dismissal_deletes_the_kept_dir(self):
        StubComfy.mode = 'gate'
        web.JOB_TIMEOUT = 0.3
        job = web.new_job('enhance', ['upscale', 'smooth', 'encode'])
        web.run_job(job, web.enhance_job, str(self.take_dir('run-d')))
        self.assertEqual(self.workdirs(), [Path(job['workdir']).name])
        web.release_resolved(job, 'dismissed', by='user')
        self.assertEqual(self.workdirs(), [])
        self.assertEqual(job['resolved']['by'], 'user')

    def test_sweep_clears_crash_leftovers_but_not_owned_or_fresh_dirs(self):
        stale = enhance.new_workdir(self.take_dir('run-old'), 'stale')
        (stale / 'raw.mkv').write_bytes(b'x')          # a leftover intermediate
        fresh = enhance.new_workdir(self.take_dir('run-new'), 'fresh')
        owned = enhance.new_workdir(self.take_dir('run-owned'), 'job-9')
        old = time.time() - 3 * 86400
        for d in (stale, owned):
            for f in [d, *d.rglob('*')]:
                os.utime(f, (old, old))
        job = web.new_job('enhance', ['upscale'])
        job['workdir'] = str(owned)                    # an unresolved job still owns this dir
        web.JOBS[job['id']] = job
        self.assertEqual(web.sweep_enhance_tmp(), [stale.name])
        self.assertTrue(fresh.is_dir() and owned.is_dir())
        self.assertFalse(stale.exists())

    # ---------------------------------------------------------------- intermediate placement

    def test_raw_frames_go_to_the_job_dir_not_a_shared_name(self):
        run = self.take_dir('run-raw', raw=True, frames=3)
        workdir = enhance.new_workdir(run, 'job-1')
        source, frames = enhance.intermediate(run, workdir)
        self.assertEqual(frames, 3)
        self.assertEqual(source, workdir / 'run-raw-raw.mkv')       # inside the per-job dir
        self.assertEqual(len(FakePopen.procs), 1)
        self.assertEqual(FakePopen.procs[0][-1], str(workdir / 'run-raw-raw.mkv'))
        self.assertFalse((enhance.COMFY_ROOT / 'input' / 'director' / 'run-raw-raw.mkv').exists())

    def test_fallback_still_chosen_when_raw_frames_are_gone(self):
        run = self.take_dir('run-mp4')                 # mp4 only: FreeVideo 0.4.9+ freed the frames
        workdir = enhance.new_workdir(run, 'job-2')
        source, frames = enhance.intermediate(run, workdir)
        self.assertEqual(source, run / 'video.mp4')
        self.assertIsNone(frames)
        self.assertEqual(list(workdir.iterdir()), [])  # no intermediate written at all
        self.assertEqual(FakePopen.procs, [])


if __name__ == '__main__':
    unittest.main()
