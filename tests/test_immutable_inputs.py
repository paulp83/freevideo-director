"""Nothing the job submits or records is re-read from the project after capture: the graph, the
uploaded picture bytes, the seed and the snapshot all freeze at build time, and a project rewritten
between build_graph and the POST /prompt changes none of them.

    python3 -m unittest discover tests      (from the repo root)

ComfyUI is the local stub of tests/test_provenance.py; the swap happens inside a wrapped
submit_prompt, i.e. in the exact window the claim is about.
"""
import json, shutil, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enhance
import jobs_db
import project as projects
import stats
import web

PROMPT = 'a cat walks left'
SWAP = 'a dog drives a bus'


class StubComfy(BaseHTTPRequestHandler):
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
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/upload/image':
            return self._json(200, {'name': 'upload.png', 'subfolder': 'director'})
        if self.path == '/prompt':
            return self._json(200, {'prompt_id': 'stub-1'})
        self._json(200, {})

    def do_GET(self):
        if self.path.startswith('/history/'):
            pid = self.path.rsplit('/', 1)[-1]
            outputs = {'9': {'gifs': [{'filename': 'take.mp4', 'subfolder': 'director', 'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': outputs}})
        self._json(200, {})


class ImmutableInputs(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-imm-'))
        self.saved = (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
                      web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS,
                      web.submit_prompt, web.upload)
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
        self.slug = projects.create('Frozen film', 'nothing', 30, '16:9',
                                    [('cat.png', b'ref-v1'), ('dog.jpg', b'fur-v1')])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, {
            'title': 'Frozen film', 'bible': 'the bible', 'music': '',
            'scenes': [{'title': 'One', 'seconds': 7, 'connect': 'cut', 'action': 'one acts'}]}))
        self.sid = projects.load(self.slug)['scenes'][0]['id']
        projects.mutate(self.slug, lambda p: p['scenes'][0].update(prompt=PROMPT, seed=1234567))

    def tearDown(self):
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS,
         web.submit_prompt, web.upload) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_every_input_freezes_before_the_submit(self):
        uploads, captured = [], {}
        real_submit, real_upload = self.saved[9], self.saved[10]

        def spy_upload(name, raw):
            uploads.append((name, bytes(raw)))
            return real_upload(name, raw)

        def spy_submit(payload):
            # rewrite the film -- prompt, seconds, seed and the ref pictures on disk -- inside the
            # window between graph capture and the POST /prompt
            projects.mutate(self.slug, lambda p: p['scenes'][0].update(prompt=SWAP, seconds=13, seed=7654321))
            for f in (projects.ROOT / self.slug / 'refs').iterdir():
                f.write_bytes(b'replaced')
            captured['body'] = json.dumps(payload)
            return real_submit(payload)

        web.upload, web.submit_prompt = spy_upload, spy_submit
        job = web.new_job('render-draft', web.scene_stage_keys('render-draft'), project=self.slug, scene=self.sid)
        web.run_job(job, web.render_job, self.slug, self.sid, 'draft')
        self.assertEqual(job['state'], 'finished', job['error'])

        body = captured['body']
        self.assertIn(PROMPT, body, 'the submitted graph carries the captured prompt')
        self.assertNotIn(SWAP, body)
        self.assertIn('1234567', body, 'the submitted graph carries the captured seed')
        self.assertNotIn('7654321', body)
        self.assertEqual(sorted(raw for _, raw in uploads), [b'fur-v1', b'ref-v1'],
                         'the bytes uploaded are the pictures as they were at build time')

        take = projects.load(self.slug)['scenes'][0]['takes'][0]
        self.assertEqual(take['seed'], 1234567, 'the take records the seed the job captured')

        snap = json.loads((projects.ROOT / self.slug / 'jobs' / (job['id'] + '.json')).read_text())
        self.assertEqual(snap['prompt'], PROMPT)
        self.assertEqual(snap['seconds'], 7)
        self.assertEqual(snap['seed'], 1234567)


if __name__ == '__main__':
    unittest.main()
