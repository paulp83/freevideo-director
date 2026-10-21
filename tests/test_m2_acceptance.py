"""M2 acceptance (update-design.md:133-141): the five dependency changes run end to end on ONE film
-- four scenes (a cut, two continuations, another cut) whose takes were recorded by the REAL render
job (immutable snapshot in jobs/, inputs kept in refs-store/) and whose cut was assembled and stored
with its fingerprint. Each case rebuilds the film from a fresh baseline:

  1. change the upstream selection of scene 1 -> only scene 2's continuity is outdated; scene 3
     stays current (the chain is informational, not a verdict), scene 4 untouched;
  2. edit scene 4's prompt -> only scene 4 is prompt_changed, and the cut stays current;
  3. replace a reference picture -> exactly the scenes whose takes submitted it are refs_changed;
  4. reorder the scenes -> continuity is re-read against the NEW previous scene;
  5. change the music bed -> the cut is outdated naming only 'music', no scene is flagged.

On top of the five: old takes, their snapshots and their media stay on disk and keep being served
after every change; an assembled cut's used_takes never move when another take finishes (only its
derived 'changes' does); a take answers the take endpoint with a snapshot that matches the bytes the
stub backend actually received (graph, seed, prompt, refs); and no derived flag ever reaches
project.json.

Nothing is re-implemented here: StubComfy / write_npy / Provenance.render come from
tests/test_provenance.py (Runs below only gives every submit its own run directory and captures what
was posted), Cuts.assemble and FakeFfmpeg from tests/test_fingerprint.py, and the per-position flag
table plus the "stored nothing" assertion from tests/test_status.py's Status.check.

Mocked: ComfyUI (the stub) and the assemble encode (fake ffmpeg -- so the cut mp4 is a placeholder
this test writes itself). Real: web.render_job through snapshot, submit and take record; the
selection / save / music endpoints; ffmpeg extracting each continuation frame out of a real
rgb.npy; and the real web.Handler answering over HTTP on a spare port.

    python3 -m unittest discover tests      (from the repo root)
"""
import base64, copy, hashlib, io, json, re, shutil, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
import urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import enhance
import jobs_db
import project as projects
import stats
import web
import test_fingerprint
import test_provenance
import test_status

# scene 1 is a cut, 2 and 3 continue the scene before them, 4 is another cut (independent)
PLAN = {'title': 'Acceptance film', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'},
                   {'title': 'Three', 'seconds': 6, 'connect': 'continue', 'action': 'three acts'},
                   {'title': 'Four', 'seconds': 6, 'connect': 'cut', 'action': 'four acts'}]
        }
PROMPTS = ('alpha', 'beta', 'gamma', 'delta')
REFS = [('cat.png', b'AAA'), ('dog.jpg', b'BBB')]
FRAME_SIZE = (96, 128)          # height, width of the fake take frames (keep the npy small)
DERIVED = {'status', 'flags', 'outdated', 'changes', 'unknown_legacy', 'downstream_of',
           'chosen', 'selected'}


class Runs(test_provenance.StubComfy):
    """The provenance stub, with the two things an acceptance run needs: a distinct output
    directory per submit (so takes, frames and runs are all separable) and a record of exactly what
    was handed to ComfyUI, so a snapshot can be compared with what execution actually used."""
    prompts = []                                # parsed POST /prompt bodies, in submit order
    uploads = []                                # (filename, bytes) given to /upload/image, in order

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/prompt':
            Runs.prompts.append(json.loads(body))
        elif self.path == '/upload/image':
            self._capture(body)
            return self._json(200, {'name': f'upload-{len(Runs.uploads) - 1}.png', 'subfolder': 'director'})
        self.rfile = io.BytesIO(body)           # let the inherited handler read the same body
        super().do_POST()

    def _capture(self, body):
        m = re.search(r'boundary=([0-9a-f]+)', self.headers.get('Content-Type', ''))
        bound = m.group(1).encode()
        for part in body.split(b'--' + bound + b'\r\n'):
            fm = re.search(rb'name="image"; filename="([^"]*)"\r\n', part)
            if not fm:
                continue
            head = part.find(b'\r\n\r\n', fm.end())   # the part's headers, then the raw picture
            raw = part[head + 4:]
            cut = raw.find(b'\r\n--' + bound)
            Runs.uploads.append((fm.group(1).decode(), raw if cut < 0 else raw[:cut]))

    def do_GET(self):
        if self.path.startswith('/history/'):
            pid = self.path.rsplit('/', 1)[-1]
            if test_provenance.StubComfy.mode == 'hang':
                return self._json(200, {})
            out = {'9': {'gifs': [{'filename': 'take.mp4', 'subfolder': f'director/{pid}', 'type': 'output'}]}}
            return self._json(200, {pid: {'status': {'completed': True}, 'outputs': out}})
        self._json(200, {})


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(('127.0.0.1', 0), Runs)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.stub_url = f'http://127.0.0.1:{cls.stub.server_address[1]}'
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)   # the real UI server
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.app.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.app.server_close()
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self.roots = []
        self.saved = (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
                      web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS)
        self.baseline()

    def tearDown(self):
        (projects.ROOT, stats.FILE, jobs_db.FILE, enhance.COMFY, enhance.COMFY_ROOT,
         web.COMFY, web.COMFY_OUTPUT, web.JOB_TIMEOUT, web.POLL_SECONDS) = self.saved
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------------- the wire

    def get(self, route):
        try:
            with urllib.request.urlopen(self.base + route, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, route, payload):
        req = urllib.request.Request(self.base + route, json.dumps(payload).encode(),
                                     {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def star(self, scene_id, take=None, auto=False):
        """Selection change through the endpoint the page uses."""
        body = {'scene': scene_id, 'auto': True} if auto else {'scene': scene_id, 'take': take}
        code, view = self.post(f'/api/p/{self.slug}/star', body)
        self.assertEqual(code, 200, view)
        return view

    def set_music(self, blob):
        code, view = self.post(f'/api/p/{self.slug}/music', {
            'file': {'name': 'song.mp3', 'data': 'data:audio/mpeg;base64,' + base64.b64encode(blob).decode()}})
        self.assertEqual(code, 200, view)

    def save(self, data):
        data = dict(data, base_rev=projects.rev_of(projects.load(self.slug)))
        code, reply = self.post(f'/api/p/{self.slug}/save', data)
        self.assertEqual(code, 200, reply)
        return reply

    # ----------------------------------------------------------------- the baseline

    def baseline(self):
        """The film: 4 scenes, one recorded take each (scene 2 and 3 continuations), scene 1 pinned
        to its take, a music bed, and one assembled film cut with its fingerprint."""
        root = Path(tempfile.mkdtemp(prefix='director-m2acc-'))
        self.roots.append(root)
        projects.ROOT = root
        stats.FILE = root / 'stats.json'
        jobs_db.FILE = root / 'jobs.db'
        enhance.COMFY = web.COMFY = self.stub_url
        enhance.COMFY_ROOT = root / 'comfy'
        web.COMFY_OUTPUT = root / 'comfy' / 'output'
        web.JOB_TIMEOUT, web.POLL_SECONDS = 120.0, 0.05
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        projects.HASH_CACHE.clear()             # the film dir is new; stale hashes must not survive
        Runs.prompts, Runs.uploads = [], []
        test_provenance.StubComfy.mode, test_provenance.StubComfy.submits = 'complete', 0

        self.slug = projects.create('Acceptance film', 'nothing', 30, '16:9', REFS)['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))

        def tune(p):                            # instructions must be final BEFORE any prompt runs
            p['bible'], p['notes'] = 'the bible', 'the notes'
            p['loras'] = projects.clean_loras([{'name': 'style.safetensors', 'strength': 0.8}])
            p['size'] = list(FRAME_SIZE[::-1])  # small frames keep the fake rgb.npy cheap
            for s, prompt in zip(p['scenes'], PROMPTS):
                s['prompt'] = prompt
        projects.mutate(self.slug, tune)
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]

        self.takes = {}                         # scene position -> [(take id, job id, run)]
        self.exec_of = {}                       # take id -> {'graph', 'uploads', 'upload_from'}
        for i in range(4):
            self.render(i)
        self.star(self.ids[0], self.takes[0][0][0])        # an explicit pick, so auto-latest cannot drift
        self.set_music(b'song-a')
        out = test_fingerprint.Cuts.assemble(self, 'film')
        projects.path(self.slug, out['file']).write_bytes(b'cut-v1')   # the fake encode wrote nothing

        p = projects.load(self.slug)
        self.recorded = [({'scene': s['id'], 'take': t['id'], 'job': t['job'],
                           'run': t['run'], 'path': t['path']})
                         for s in p['scenes'] for t in s['takes'] if t.get('id')]
        self.cut_file = out['file']
        self.cut_bytes = projects.path(self.slug, out['file']).read_bytes()
        self.cut_files = [self.cut_file]
        self.entry0 = copy.deepcopy(p['outputs']['film'])

    def render(self, i):
        """Scene i through the real render job; the take dir gets what FreeVideo would leave, so a
        later continuation can read its last frame for real."""
        up, pr = len(Runs.uploads), len(Runs.prompts)
        job = test_provenance.Provenance.render(self, self.ids[i])
        run = job['take']['run']
        d = Path(run)
        (d / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        test_provenance.write_npy(d / 'video.artifacts' / 'rgb.npy', 3, *FRAME_SIZE)
        (d / 'take.mp4').write_bytes(b'mp4')              # the file the stub history advertises
        tid = projects.load(self.slug)['scenes'][i]['takes'][-1]['id']
        self.exec_of[tid] = {'graph': Runs.prompts[pr], 'uploads': Runs.uploads[up:], 'upload_from': up}
        self.takes.setdefault(i, []).append((tid, job['id'], run))
        return tid, job['id'], run

    # ------------------------------------------------------------------- assertions

    def check(self, case, expect, downstream=None):
        """The flag table of tests/test_status.py: every scene's every flag, position by position,
        plus scene_status proving it stores nothing."""
        test_status.Status.check(self, case, expect, downstream)

    def statuses(self):
        p = projects.load(self.slug)
        return {s['id']: projects.scene_status(p, s) for s in p['scenes']}

    def view(self):
        """GET the project over HTTP, then prove the GET wrote nothing derived to disk."""
        f = projects.path(self.slug, 'project.json')
        before = f.read_bytes()
        code, raw = self.get(f'/api/p/{self.slug}')
        self.assertEqual(code, 200)
        self.assertEqual(f.read_bytes(), before, 'the GET wrote derived state into project.json')
        self.no_derived(json.loads(before.decode()), '$')
        body = json.loads(raw)
        for s in body['scenes']:                # derived state IS on the wire
            self.assertIn('status', s)
            self.assertIn('chosen', s)
        for out in body['outputs'].values():
            self.assertIn('outdated', out)
            self.assertIn('changes', out)
        return body

    def no_derived(self, node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                self.assertNotIn(key, DERIVED, f'{where}.{key}: a derived flag was persisted')
                self.no_derived(value, f'{where}.{key}')
        elif isinstance(node, list):
            for n, value in enumerate(node):
                self.no_derived(value, f'{where}[{n}]')

    def history_intact(self, case):
        """Every baseline take, snapshot, media file and cut is still there and still served."""
        p = projects.load(self.slug)
        scenes = {s['id']: s for s in p['scenes']}
        for rec in self.recorded:
            scene = scenes[rec['scene']]
            take = projects.find_take(scene, rec['take'])
            self.assertIsNotNone(take, f'{case}: take {rec["take"]} was deleted')
            self.assertEqual((take['run'], take['path']), (rec['run'], rec['path']),
                             f'{case}: take {rec["take"]} moved')
            self.assertTrue(Path(rec['path']).is_file(), f'{case}: {rec["path"]} is gone')
            snap, result = projects.read_job(self.slug, rec['job'])
            self.assertIsNotNone(snap, f'{case}: snapshot of {rec["job"]} is gone')
            self.assertIsNotNone(result, f'{case}: result of {rec["job"]} is gone')
            code, raw = self.get(f'/api/p/{self.slug}/take?id={rec["take"]}')
            self.assertEqual(code, 200, f'{case}: take endpoint lost a historical take')
            self.assertEqual(json.loads(raw)['snapshot']['id'], rec['job'], f'{case}: wrong snapshot')
        for name in self.cut_files:             # every cut ever made, including superseded ones
            code, raw = self.get(f'/files/{self.slug}/{name}')
            self.assertEqual(code, 200, f'{case}: {name} is no longer served')
        self.assertEqual(self.get(f'/files/{self.slug}/{self.cut_file}')[1], self.cut_bytes,
                         f'{case}: a historical cut changed')

    # ------------------------------------------------------------------- the five

    def test_acceptance_table(self):
        for name, run_case in (('upstream selection', self.case_selection),
                               ('prompt edit', self.case_prompt),
                               ('reference replaced', self.case_refs),
                               ('scene reorder', self.case_reorder),
                               ('music replaced', self.case_music)):
            with self.subTest(case=name):
                self.baseline()                 # every case starts from the same fresh film
                self.check(f'{name}: baseline', {})
                run_case()
                self.history_intact(name)
                self.view()                     # and nothing derived leaked, after every GET

    def case_selection(self):
        """Change an upstream selection: only the scene that starts from it is outdated."""
        tid_a, _, _ = self.takes[0][0]
        tid_b, _, _ = self.render(0)            # another take of scene 1; the pin keeps it unused
        self.check('scene 1 has a second take, still pinned to the first', {})
        self.star(self.ids[0], tid_b)
        self.check('upstream selection moved', {1: {'continuity_outdated': 'changed'}})
        st = self.statuses()
        self.assertIn(f'source take {tid_a} -> {tid_b}', st[self.ids[1]]['flags']['continuity_outdated']['reason'])
        self.assertEqual(st[self.ids[1]]['outdated'], ['continuity_outdated'])
        self.assertEqual(st[self.ids[2]]['outdated'], [])           # downstream: informational only
        self.assertEqual([e['id'] for e in st[self.ids[1]]['downstream_of']], [self.ids[2]])
        self.assertEqual(st[self.ids[3]]['flags']['continuity_outdated']['state'], 'not_applicable')
        cut = self.view()['outputs']['film']
        self.assertEqual(cut['changes'], ['takes'])                 # the cut names just that
        self.star(self.ids[0], tid_a)                               # and it clears again
        self.check('upstream selection changed back', {})
        self.assertEqual(self.view()['outputs']['film']['changes'], [])

    def case_prompt(self):
        """Edit a prompt: exactly that scene, and the assembled cut is untouched."""
        self.save({'scenes': [{'id': self.ids[3], 'prompt': 'delta revised'}], 'scenes_delta': True})
        self.check('scene 4 prompt edited', {3: {'prompt_changed': 'changed'}})
        st = self.statuses()
        self.assertEqual(st[self.ids[3]]['outdated'], ['prompt_changed'])
        self.assertEqual(st[self.ids[3]]['flags']['prompt_changed']['reason'], 'prompt text')
        self.assertEqual([st[i]['outdated'] for i in self.ids[:3]], [[], [], []])
        cut = self.view()['outputs']['film']
        self.assertEqual((cut['outdated'], cut['changes']), (False, []))   # prompts are not in a cut

    def case_refs(self):
        """Replace a reference picture: exactly the scenes whose takes submitted it."""
        ref = projects.path(self.slug, 'refs', 'picture-1.png')
        old = ref.read_bytes()
        old_sha = hashlib.sha256(old).hexdigest()
        ref.write_bytes(b'AAAA-REPLACED')
        self.check('picture-1 replaced', {0: {'refs_changed': 'changed'}, 3: {'refs_changed': 'changed'}})
        st = self.statuses()
        for i in (0, 3):
            self.assertIn('picture-1.png content', st[self.ids[i]]['flags']['refs_changed']['reason'])
        for i in (1, 2):      # continuations started from a frame, not from the pictures
            self.assertEqual(st[self.ids[i]]['flags']['refs_changed']['state'], 'same')
        kept = projects.path(self.slug, projects.REFS_STORE, f'{old_sha}.png')
        self.assertEqual(kept.read_bytes(), old)                     # the input the hash points at survives
        self.assertEqual(self.view()['outputs']['film']['changes'], [])   # a cut is not built of prompts

    def case_reorder(self):
        """Reorder scenes: continuity is judged against the new previous scene."""
        p = projects.load(self.slug)
        order = [p['scenes'][0], p['scenes'][3], p['scenes'][1], p['scenes'][2]]   # One, Four, Two, Three
        self.save({'scenes': [{'id': s['id']} for s in order]})
        self.check('scenes reordered', {2: {'continuity_outdated': 'changed'}})
        st = self.statuses()
        self.assertIn(f'previous scene {self.ids[0]} -> {self.ids[3]}',
                      st[self.ids[1]]['flags']['continuity_outdated']['reason'])
        self.assertEqual(st[self.ids[2]]['outdated'], [])           # its own source still stands
        self.assertEqual(st[self.ids[3]]['outdated'], [])           # the moved scene is not outdated
        self.assertEqual([e['id'] for e in st[self.ids[0]]['downstream_of']], [])
        self.assertEqual([e['id'] for e in st[self.ids[3]]['downstream_of']], [self.ids[1], self.ids[2]])
        self.assertEqual(self.view()['outputs']['film']['changes'], ['takes'])

    def case_music(self):
        """Change the music: the cut is outdated naming music, no scene is flagged."""
        self.set_music(b'song-b')
        cut = self.view()['outputs']['film']
        self.assertEqual((cut['outdated'], cut['changes']), (True, ['music']))
        self.check('music bed replaced', {})
        self.assertEqual([st['outdated'] for st in self.statuses().values()], [[], [], [], []])
        time.sleep(1.05)                       # cut names are stamped to the second
        new = test_fingerprint.Cuts.assemble(self, 'film')
        projects.path(self.slug, new['file']).write_bytes(b'cut-v2')
        self.cut_files.append(new['file'])
        fresh = self.view()['outputs']['film']
        self.assertEqual((fresh['outdated'], fresh['changes']), (False, []))
        self.assertEqual(fresh['used_takes'], self.entry0['used_takes'])   # selection never moved
        self.assertNotEqual(fresh['file'], self.cut_file)
        self.assertTrue(projects.path(self.slug, self.cut_file).is_file())  # the old cut survived


# ------------------------------------------------------------------- the rest of the acceptance list

    def test_export_selection_holds_while_other_takes_finish(self):
        """A finished take never moves an existing cut: the stored record is frozen, only the
        derived 'changes' reacts, and it names only what really moved."""
        code, _ = self.post(f'/api/p/{self.slug}/render', {'scene': self.ids[0], 'preset': 'draft'})
        self.assertEqual(code, 200)
        self.wait_for_idle()
        p = projects.load(self.slug)
        self.assertEqual(p['outputs']['film'], self.entry0, 'a take finishing changed the stored cut')
        self.assertEqual(self.view()['outputs']['film']['changes'], [])   # scene 1 is pinned: nothing moved

        code, _ = self.post(f'/api/p/{self.slug}/render', {'scene': self.ids[1], 'preset': 'draft'})
        self.assertEqual(code, 200)
        self.wait_for_idle()
        p = projects.load(self.slug)
        self.assertEqual(p['outputs']['film'], self.entry0, 'a take finishing changed the stored cut')
        cut = self.view()['outputs']['film']
        self.assertEqual(cut['changes'], ['takes'])                # only the auto scene's pick moved
        self.assertEqual(cut['used_takes'], self.entry0['used_takes'])
        self.assertEqual(projects.cut_fingerprint(p, self.entry0['used_takes'], 'film'),
                         self.entry0['fingerprint'])               # the recorded values still match
        self.assertNotEqual(projects.current_cut_fingerprint(p, 'film'), self.entry0['fingerprint'])
        moved = [(u['scene'], u['take']) for u in projects.current_used_takes(p, 'film')]
        self.assertEqual([u for u, v in zip(self.entry0['used_takes'], moved) if u['take'] != v[1]],
                         [{'scene': self.ids[1], 'take': self.entry0['used_takes'][1]['take'],
                           'run': self.entry0['used_takes'][1]['run'], 'selection': 'auto'}])
        st = self.statuses()
        self.assertEqual(st[self.ids[2]]['outdated'], ['continuity_outdated'])  # scene 3 sees scene 2 move
        self.assertEqual(st[self.ids[0]]['outdated'], [])
        self.assertEqual(st[self.ids[3]]['outdated'], [])
        self.history_intact('takes finishing')

    def wait_for_idle(self):
        end = time.time() + 30
        while time.time() < end and not all(j['done'] for j in web.JOBS.values()):
            time.sleep(0.05)
        self.assertTrue(all(j['done'] for j in web.JOBS.values()), 'a render never finished')

    def test_a_take_shows_the_inputs_that_actually_ran(self):
        """The take endpoint answers with the snapshot, and it matches what execution handed over:
        the exact submitted graph, its seed/prompt, and the reference bytes that were uploaded."""
        tid, jid, run = self.takes[0][0]
        cont, cj, crun = self.takes[1][0]
        code, raw = self.get(f'/api/p/{self.slug}/take?id={tid}')
        self.assertEqual(code, 200)
        got = json.loads(raw)
        self.assertEqual(got['provenance'], 'recorded')
        self.assertEqual(got['scene'], self.ids[0])
        snap, result = got['snapshot'], got['result']
        used = self.exec_of[tid]
        self.assertEqual(snap['id'], jid)
        self.assertEqual(snap['kind'], 'render-draft')
        self.assertEqual((snap['project'], snap['scene'], snap['take_id']), (self.slug, self.ids[0], tid))
        self.assertEqual(snap['graph'], used['graph']['prompt'])          # submitted verbatim
        gen = next(n for n in snap['graph'].values() if n['class_type'] == 'FreeVideoGenerate')['inputs']
        self.assertEqual(gen['text'], snap['prompt'])
        self.assertEqual((gen['seed'], gen['width'], gen['height'], gen['seconds']),
                         (snap['seed'], snap['width'], snap['height'], snap['seconds']))
        scene = projects.load(self.slug)['scenes'][0]
        self.assertEqual(gen['seed'], scene['seed'])
        self.assertEqual(gen['text'], PROMPTS[0])
        self.assertEqual(snap['seed'], projects.find_take(scene, tid)['seed'])
        self.assertEqual(snap['loras'], projects.clean_loras([{'name': 'style.safetensors', 'strength': 0.8}]))
        self.assertEqual(snap['instructions'], projects.instructions_of(projects.load(self.slug)))

        loaded = {n['inputs']['image'] for n in snap['graph'].values() if n['class_type'] == 'LoadImage'}
        expected = [f'director/upload-{k}.png' for k in range(used['upload_from'],
                                                              used['upload_from'] + len(used['uploads']))]
        self.assertEqual(loaded, set(expected))
        self.assertEqual([r['index'] for r in snap['refs']], list(range(len(REFS))))
        for n, (name, raw_bytes) in enumerate(used['uploads']):
            self.assertEqual(snap['refs'][n]['name'], name)               # ref order = upload order
            self.assertEqual(snap['refs'][n]['sha256'], hashlib.sha256(raw_bytes).hexdigest())
            self.assertEqual(snap['refs'][n]['bytes'], len(raw_bytes))
            self.assertEqual(projects.path(self.slug, snap['refs'][n]['store']).read_bytes(), raw_bytes)
        self.assertEqual(result['prompt_ids'], [used['graph']['prompt_id']])   # the id WE supplied came back
        self.assertEqual((result['run'], result['path'], result['take_id']), (run, str(Path(run) / 'take.mp4'), tid))

        code, raw = self.get(f'/api/p/{self.slug}/take?id={cont}')
        c = json.loads(raw)
        self.assertEqual(c['take']['job'], cj)
        first = c['snapshot']['first']
        name, frame = self.exec_of[cont]['uploads'][0]
        self.assertEqual(name, f'{Path(run).name}-last.png')              # the previous take's last frame
        self.assertEqual(projects.path(self.slug, first['store']).read_bytes(), frame)  # kept, not hashed
        self.assertEqual(first['sha256'], hashlib.sha256(frame).hexdigest())
        self.assertEqual(first['source'], {'scene': self.ids[0], 'take': tid, 'run': run})
        self.assertEqual(c['take']['source_take'], {'scene': self.ids[0], 'take': tid, 'run': run,
                                                    'frame_sha256': first['sha256']})
        png = projects.path(self.slug, 'frames', f'{Path(run).name}-last.png')
        self.assertEqual(png.read_bytes(), frame)                         # the frame the run really used
        self.assertEqual(c['snapshot']['prompt'], projects.ensure_i2va(PROMPTS[1]))
        code, raw = self.get(f'/api/p/{self.slug}/take?id=not-a-take')
        self.assertEqual(code, 404)
        f = projects.path(self.slug, 'project.json')
        self.no_derived(json.loads(f.read_text()), '$')


if __name__ == '__main__':
    unittest.main()
