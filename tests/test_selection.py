"""Take selection: an explicit pick per scene beats the automatic latest-take fallback, a bare
`star` from an old project reads as an explicit pick until the first write folds it in (a GET
never rewrites), assemble records which takes went into each cut, and a continuation take names
the take (and frame hash) its first frame came from.

    python3 -m unittest discover tests      (from the repo root)

ComfyUI is the local stub of tests/test_provenance.py; assemble() runs against a fake ffmpeg.
Only the continuation tests use the real ffmpeg binary.
"""
import hashlib, itertools, json, shutil, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enhance
import jobs_db
import project as projects
import stats
import web


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
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/upload/image':
            return self._json(200, {'name': f'upload-{StubComfy.submits}.png', 'subfolder': 'director'})
        if self.path == '/prompt':
            StubComfy.submits += 1
            return self._json(200, {'prompt_id': f'stub-{StubComfy.submits}'})
        self._json(200, {})

    def do_GET(self):
        if self.path.startswith('/history/'):
            pid = self.path.rsplit('/', 1)[-1]
            if StubComfy.mode == 'hang':
                return self._json(200, {})     # never finishes
            outputs = {'9': {'gifs': [{'filename': 'take.mp4', 'subfolder': 'director', 'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': outputs}})
        self._json(200, {})


PLAN = {'title': 'Selection test', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'}]}


def write_npy(path, frames, height, width):
    """A minimal uint8 .npy (v1 header, 64-byte aligned) as FreeVideo leaves rgb.npy."""
    d = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({frames}, {height}, {width}, 3), }}\n"
    d += '\n' * ((-10 - len(d)) % 64)
    path.write_bytes(b'\x93NUMPY\x01\x00' + len(d).to_bytes(2, 'little') + d.encode('latin1')
                     + b'\x11' * (frames * height * width * 3))


class FakeFfmpeg:
    """Stands in for the subprocess module inside project.py: ffprobe answers a 6 s silent clip
    and the final encode always succeeds, without ffmpeg ever running."""

    PIPE = subprocess.PIPE

    def run(self, cmd, **kw):
        stdout = '6.000000\n' if 'format=duration' in cmd else ''
        return subprocess.CompletedProcess(cmd, 0, stdout, '')

    def Popen(self, cmd, **kw):
        return _FakeProc(cmd)


class _FakeProc:
    """Stands in for the Popen of a finished encode: poll answers 0 without a child process."""
    def __init__(self, cmd):
        self.cmd, self.returncode = cmd, None
    def poll(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode
    def kill(self):
        self.returncode = -9
    def communicate(self):
        return ('', '')

class Selection(unittest.TestCase):
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
        self.root = Path(tempfile.mkdtemp(prefix='director-sel-'))
        StubComfy.mode, StubComfy.submits = 'complete', 0
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
        self.seq = itertools.count(1)
        self.slug = projects.create('Sel film', 'nothing', 30, '16:9',
                                    [('cat.png', b'AAA'), ('dog.jpg', b'BBB')])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]
        projects.mutate(self.slug, lambda p: [s.__setitem__('prompt', 'a cat walks left')
                                              for s in p['scenes']])

    def tearDown(self):
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS) = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def render(self, scene_id, preset='draft'):
        kind = 'render-' + preset
        job = web.new_job(kind, web.scene_stage_keys(kind), project=self.slug, scene=scene_id)
        web.run_job(job, web.render_job, self.slug, scene_id, preset)
        self.assertEqual(job['state'], 'finished', job['error'])
        return job

    def add_take(self, scene_ix=0, tid=None, run=None):
        """Append a finished take record the way a render leaves it; no tid means a legacy take."""
        run = run or f'/comfy/output/director/run-{next(self.seq)}'
        rec = {'run': run, 'path': run + '/video.mp4', 'url': '/files/x', 'preset': 'draft',
               'seed': 7, 'created': time.time(), 'final': None, 'final_url': None, 'variant': None}
        if tid:
            rec.update({'id': tid, 'job': 'job-' + tid, 'provenance': 'recorded'})
        projects.mutate(self.slug, lambda p: p['scenes'][scene_ix]['takes'].append(rec))
        return rec

    def take_dir(self, name):
        """A take dir FreeVideo leaves behind: the mp4, with the raw frames gone."""
        run = web.COMFY_OUTPUT / 'director' / name
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        (run / 'video.mp4').write_bytes(b'mp4')
        return str(run)

    # -------------------------------------------------------------- resolution

    def test_auto_choice_follows_the_latest_take(self):
        self.add_take(0)
        b = self.add_take(0)
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual(projects.selection_of(s), (None, 'auto'))
        self.assertEqual(projects.chosen(s)['run'], b['run'])

    def test_explicit_choice_survives_newer_takes(self):
        a = self.add_take(0, tid='take-aaaa')
        self.add_take(0)
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'take-aaaa'))
        self.add_take(0)                                       # a render finishes afterwards
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual(projects.chosen(s)['id'], 'take-aaaa')
        self.assertEqual(projects.selection_of(s), ('take-aaaa', 'explicit'))
        self.assertEqual(s['star'], a['run'])                  # kept in step for old readers

    def test_selection_by_run_for_a_legacy_take_invents_no_id(self):
        legacy = self.add_take(0, run='/comfy/output/director/run-old')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], legacy['run']))
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual(s['selected_take'], legacy['run'])
        self.assertEqual(s['selection'], 'explicit')
        self.assertEqual(projects.chosen(s)['run'], legacy['run'])
        with self.assertRaises(projects.ProjectError):         # an unknown ref is refused, not stored
            projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'take-nope'))
        after = projects.load(self.slug)['scenes'][0]              # the refusal changed nothing
        self.assertEqual((after['selection'], after['selected_take'], after['star']),
                         ('explicit', legacy['run'], legacy['run']))

    def test_auto_release_returns_the_scene_to_its_latest_take(self):
        self.add_take(0, tid='take-aaaa')
        b = self.add_take(0)
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'take-aaaa'))
        projects.mutate(self.slug, lambda p: projects.set_auto(p, self.ids[0]))
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual(projects.selection_of(s), (None, 'auto'))
        self.assertIsNone(s['star'])
        self.assertEqual(projects.chosen(s)['run'], b['run'])

    # -------------------------------------------------------------- legacy star

    def _legacy(self, tid=None):
        """Rewrite scene 0 as an old project file has it: takes plus a bare star, no selection keys."""
        run = '/comfy/output/director/run-starred'
        take = {'run': run, 'path': run + '/video.mp4', 'url': '/files/x', 'preset': 'draft',
                'seed': 1, 'created': 0.0, 'final': None, 'final_url': None, 'variant': None}
        if tid:
            take.update({'id': tid, 'job': 'job-1', 'provenance': 'recorded'})
        def apply(p):
            s = p['scenes'][0]
            s['takes'] = [take]
            s['star'] = run
            del s['selection']
            del s['selected_take']
        projects.mutate(self.slug, apply)
        return take

    def test_old_project_star_resolves_and_a_get_rewrites_nothing(self):
        legacy = self._legacy()
        f = projects.path(self.slug, 'project.json')
        before = f.read_bytes()
        self.assertEqual(projects.chosen(projects.load(self.slug)['scenes'][0])['run'], legacy['run'])
        s = web.project_view(self.slug)['scenes'][0]           # the GET path
        self.assertEqual((s['selection'], s['selected_take']), ('explicit', legacy['run']))
        self.assertEqual([t['selected'] for t in s['takes']], [True])
        self.assertEqual(f.read_bytes(), before)               # old files stay byte for byte

    def test_star_folds_into_the_selection_fields_on_the_first_write(self):
        legacy = self._legacy(tid='take-aaaa')
        projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('title', 'renamed'))
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual((s['selection'], s['selected_take']), ('explicit', 'take-aaaa'))  # the id wins
        self.assertEqual(s['star'], legacy['run'])             # star stays for older readers

    # -------------------------------------------------------------- assemble

    def test_assemble_records_the_used_takes_and_the_record_never_moves(self):
        a = self.add_take(0, tid='take-aaaa')
        self.add_take(0)                                       # newer, but scene 0 is explicit on a
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'take-aaaa'))
        legacy = self.add_take(1, run='/comfy/output/director/run-legacy')   # scene 1: auto, legacy take
        saved = projects.subprocess
        projects.subprocess = FakeFfmpeg()
        try:
            out = projects.assemble(projects.load(self.slug), 'animatic')
        finally:
            projects.subprocess = saved
        self.assertEqual(out['used_takes'], [
            {'scene': self.ids[0], 'take': 'take-aaaa', 'run': a['run'], 'selection': 'explicit'},
            {'scene': self.ids[1], 'take': legacy['run'], 'run': legacy['run'], 'selection': 'auto'}])
        projects.mutate(self.slug, lambda p: p['outputs'].__setitem__('animatic', out))
        self.add_take(0)                                       # renders finish after the export
        new = self.add_take(1)
        stored = projects.load(self.slug)['outputs']['animatic']['used_takes']
        self.assertEqual(stored, out['used_takes'])            # the export still names what it cut
        self.assertEqual(projects.chosen(projects.load(self.slug)['scenes'][1])['run'], new['run'])

    def test_a_take_that_lands_mid_assemble_is_not_in_the_cut(self):
        a = self.add_take(0, tid='take-aaaa')
        b = self.add_take(1, tid='take-bbbb')
        late = {'id': 'take-late', 'job': 'job-late', 'provenance': 'recorded',
                'run': '/comfy/output/director/run-late', 'path': '/comfy/output/director/run-late/video.mp4',
                'url': '/files/x', 'preset': 'draft', 'seed': 9, 'created': 0.0,
                'final': None, 'final_url': None, 'variant': None}
        class MidCut(FakeFfmpeg):
            def __init__(self, slug):
                self.slug, self.cmds, self.fired = slug, [], False
            def run(self, cmd, **kw):
                self.cmds.append(cmd)
                if not self.fired and 'format=duration' in cmd:
                    self.fired = True                          # a render finishes while ffmpeg is mid-cut
                    projects.mutate(self.slug, lambda p: p['scenes'][1]['takes'].append(late))
                return super().run(cmd, **kw)
        fake = MidCut(self.slug)
        saved = projects.subprocess
        projects.subprocess = fake
        try:
            out = projects.assemble(projects.load(self.slug), 'animatic')
        finally:
            projects.subprocess = saved
        joined = ' '.join(' '.join(c) for c in fake.cmds)
        self.assertNotIn('run-late', joined, 'every ffmpeg input names a take frozen at the start')
        self.assertIn(a['run'], joined)
        self.assertIn(b['run'], joined)
        self.assertEqual([u['take'] for u in out['used_takes']], ['take-aaaa', 'take-bbbb'])

    def test_cancelling_mid_encode_kills_ffmpeg_and_records_no_cut(self):
        self.add_take(0, tid='take-aaaa')
        self.add_take(1, tid='take-bbbb')
        class Watching(FakeFfmpeg):
            def __init__(self):
                self.procs = []
            def Popen(self, cmd, **kw):
                class Blocking:                                   # the encode child stays alive
                    def __init__(self):
                        self.returncode = None
                    def poll(self):
                        return self.returncode
                    def kill(self):
                        self.returncode = -9
                    def communicate(self):
                        return ('', '')
                p = Blocking()
                self.procs.append(p)
                return p
        fake, polls = Watching(), itertools.count()
        saved = projects.subprocess
        projects.subprocess = fake
        try:                                                   # cancel flips on after the first checkpoint
            self.assertRaises(projects.ProjectError, projects.assemble,
                              projects.load(self.slug), 'animatic', lambda: next(polls) > 0)
        finally:
            projects.subprocess = saved
        self.assertTrue(fake.procs and fake.procs[-1].returncode == -9, 'the live encode is killed')
        self.assertIsNone(projects.load(self.slug)['outputs'].get('animatic'))

    # --------------------------------------------------- renders (stub ComfyUI) and endpoint

    def test_a_finished_render_never_moves_an_explicit_pick(self):
        self.render(self.ids[0])
        t1 = projects.load(self.slug)['scenes'][0]['takes'][0]
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], t1['id']))
        self.render(self.ids[0])                               # a second take for the same scene
        s = projects.load(self.slug)['scenes'][0]
        self.assertEqual(len(s['takes']), 2)
        self.assertEqual(projects.chosen(s)['id'], t1['id'])
        view = web.project_view(self.slug)['scenes'][0]
        self.assertEqual((view['selection'], view['selected_take']), ('explicit', t1['id']))
        self.assertEqual([t['selected'] for t in view['takes']], [True, False])

    def test_star_endpoint_selects_by_id_and_auto_releases(self):
        self.render(self.ids[0])
        self.render(self.ids[0])
        t1, t2 = projects.load(self.slug)['scenes'][0]['takes']
        legacy = self.add_take(1, run='/comfy/output/director/run-old')
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)   # the real handler, spare port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{srv.server_address[1]}'

        def post(body):
            req = urllib.request.Request(f'{base}/api/p/{self.slug}/star', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        try:
            s = next(x for x in post({'scene': self.ids[0], 'take': t1['id']})['scenes']
                     if x['id'] == self.ids[0])
            self.assertEqual((s['selection'], s['selected_take']), ('explicit', t1['id']))
            stored = projects.load(self.slug)['scenes'][0]
            self.assertEqual((stored['selection'], stored['star']), ('explicit', t1['run']))
            s = next(x for x in post({'scene': self.ids[1], 'run': legacy['run']})['scenes']  # old body
                     if x['id'] == self.ids[1])                # shape still works: addressed by run
            self.assertEqual((s['selection'], s['selected_take']), ('explicit', legacy['run']))
            with self.assertRaises(urllib.error.HTTPError) as bad:
                post({'scene': self.ids[0], 'take': 'no-such-take'})
            self.assertEqual(bad.exception.code, 400)
            s = next(x for x in post({'scene': self.ids[0], 'auto': True})['scenes']
                     if x['id'] == self.ids[0])
            self.assertEqual((s['selection'], s['selected_take']), ('auto', None))
            self.assertEqual(projects.chosen(projects.load(self.slug)['scenes'][0])['id'], t2['id'])
        finally:
            srv.shutdown()
            srv.server_close()

    # --------------------------------------------------- continuation source_take

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg needed for the continuation frame')
    def test_continuation_take_record_names_its_source_take_and_frame_hash(self):
        self.render(self.ids[0])
        take0 = projects.load(self.slug)['scenes'][0]['takes'][0]
        self.assertIsNone(take0['source_take'])                # a plain cut has no source
        npy = web.COMFY_OUTPUT / 'director' / 'video.artifacts' / 'rgb.npy'
        npy.parent.mkdir(parents=True, exist_ok=True)
        write_npy(npy, 1, 2, 2)
        self.render(self.ids[1])
        take1 = projects.load(self.slug)['scenes'][1]['takes'][0]
        png = projects.path(self.slug, 'frames', 'director-last.png')
        self.assertEqual(take1['source_take'],
                         {'scene': self.ids[0], 'take': take0['id'], 'run': take0['run'],
                          'frame_sha256': hashlib.sha256(png.read_bytes()).hexdigest()})

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg needed for the continuation frame')
    def test_continuation_from_a_legacy_take_names_the_run(self):
        run = self.take_dir('run-legacy')
        write_npy(Path(run) / 'video.artifacts' / 'rgb.npy', 1, 2, 2)
        self.add_take(0, run=run)                              # scene 0 has only this legacy take
        self.render(self.ids[1])
        take1 = projects.load(self.slug)['scenes'][1]['takes'][0]
        png = projects.path(self.slug, 'frames', 'run-legacy-last.png')
        self.assertEqual(take1['source_take'],
                         {'scene': self.ids[0], 'take': run, 'run': run,
                          'frame_sha256': hashlib.sha256(png.read_bytes()).hexdigest()})
