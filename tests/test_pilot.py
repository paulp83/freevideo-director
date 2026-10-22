"""The M6 pilot tooling (pilot.py): the 12-slot manifest is immutable and resumable, renders go
through web.py's tracked submit path ONE at a time (failures recorded, unknown stops the run),
the review clips are blinded with real ffmpeg re-encodes, and the report refuses unrated clips
and never prints prompt-length style numbers.

    python3 -m unittest discover tests      (from the repo root)

Both services are in-thread HTTP stubs (ComfyUI and the director model, the test_eval stub
reused); the only real binary touched is ffmpeg, which makes the tiny take clip the stubs
"render" and performs the actual blinding re-encodes. No real ComfyUI, model or GPU.
"""
import json, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import config, enhance, jobs_db, pilot, readiness, stats, web
from test_eval import ModelStub

SENTINEL = 'SENTINEL-PROMPT-TEXT'
GOOD = (ROOT / 'tests' / 'fixtures' / 'h3' / 't2va-boat.txt').read_text().strip()
REF2VA = (ROOT / 'tests' / 'fixtures' / 'h3' / 'ref2va-lightsaber.txt').read_text().strip()
SECRETS = ('simple', 'director', 'act-test', 'talk-test', 'face-test', 'raincoat', 'paramedic',
           'mug', 'labelled', '-s5-', '-s7-', 'SECRET-TAKE-META', SENTINEL)


class DirectorStub(ModelStub):
    """ModelStub, but replies with the fixture matching the mode asked for (10-second scenes)."""
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        mode = type(self).mode
        if mode == 'http500':
            self.send_error(500, 'stub model down')
            return
        if mode == 'malformed':
            self._reply(200, b'not json at all')
            return
        # 'Mode: <label>' starts the user message; the skill text may mention other modes
        content = '' if mode == 'empty' else (
            REF2VA if 'Mode: full-reference Ref2VA' in raw.decode('utf-8', 'replace') else GOOD)
        self._reply(200, json.dumps({'choices': [{'message': {'content': content}}],
                                     'usage': {'completion_tokens': 900}}).encode())


SCENES = {
    'kind': 'freevideo-pilot-scenes', 'schema': 1,
    'fixtures': {
        'pilot-photo': {'kind': 'private', 'file': 'pilot-photo-test.png'},
        'pilot-absent': {'kind': 'private', 'file': 'pilot-absent-test.jpg',
                         'env': 'PILOT_PORTRAIT_IMAGE'}},
    'scenes': [
        {'id': 'act-test', 'title': 'A', 'seconds': 10, 'refs': [], 'private_variant': None,
         'idea': 'A woman in a raincoat sprints, leaps and rolls over a fence.', 'why': 'w'},
        {'id': 'talk-test', 'title': 'B', 'seconds': 10, 'refs': [], 'private_variant': None,
         'idea': 'A paramedic says quietly: I called her. She knows now.', 'why': 'w'},
        {'id': 'face-test', 'title': 'C', 'seconds': 10, 'refs': ['pilot-photo'],
         'private_variant': 'pilot-absent',
         'idea': 'A hand lifts the labelled mug from [Picture 1]; the label stays readable.',
         'why': 'w'}]}


class ComfyStub(BaseHTTPRequestHandler):
    """The video backend at the exact endpoints web.py's render path uses. Modes:
    ok / execfail (the fail_nth-th submitted prompt dies in history) / silent (never lands)."""
    mode = 'ok'
    fail_nth = 1

    @classmethod
    def reset(cls):
        cls.posts, cls.uploads, cls.start_ms, cls.failed = [], [], {}, []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/system_stats':
            return self._json(200, {'system': {'comfyui_version': 'stub', 'os': 'linux'}})
        if self.path == '/object_info':
            return self._json(200, {c: {} for c in ('LoadImage', 'FreeVideoReference',
                                                    'FreeVideoGenerate', 'FreeVideoLoRAStack')})
        if self.path.startswith('/history/'):
            pid = self.path.split('/', 2)[2]
            if type(self).mode == 'silent':
                return self._json(200, {})
            if pid in type(self).failed:
                return self._json(200, {pid: {'status': {
                    'completed': False,
                    'messages': [['execution_error', {'prompt_id': pid,
                                                       'exception_message': 'stub boom'}]]}}})
            t0 = type(self).start_ms.get(pid, int(time.time() * 1000))
            return self._json(200, {pid: {
                'status': {'completed': True, 'messages': [
                    ['execution_start', {'prompt_id': pid, 'timestamp': t0}],
                    ['execution_success', {'prompt_id': pid, 'timestamp': t0 + 4200}]]},
                'outputs': {'9': {'gifs': [{'filename': 'pilot_take.mp4', 'subfolder': '',
                                            'type': 'output'}]}}}})
        return self._json(404, {'error': 'no ' + self.path})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path == '/upload/image':
            type(self).uploads.append(raw)
            return self._json(200, {'name': 'pilot-photo-test.png', 'subfolder': 'director'})
        if self.path == '/prompt':
            payload = json.loads(raw)
            type(self).posts.append(payload)
            pid = payload['prompt_id']
            type(self).start_ms[pid] = int(time.time() * 1000)
            if type(self).mode == 'execfail' and len(type(self).posts) == type(self).fail_nth:
                type(self).failed.append(pid)
            return self._json(200, {'prompt_id': pid})
        return self._json(404, {'error': 'no ' + self.path})


class PilotCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-pilot-'))
        self.comfy = self.root / 'comfy'
        (self.comfy / 'output').mkdir(parents=True)
        self.take = self.comfy / 'output' / 'pilot_take.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                        '-c:v', 'libx264', '-c:a', 'aac',
                        '-metadata', 'title=SECRET-TAKE-META', '-shortest', str(self.take)],
                       check=True, capture_output=True)
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=red:size=8x8:duration=1', '-frames:v', '1',
                        str(self.fixtures / 'pilot-photo-test.png')], check=True,
                       capture_output=True)
        (self.root / 'scenes.json').write_text(json.dumps(SCENES))
        ComfyStub.reset()
        ComfyStub.mode = 'ok'
        ComfyStub.fail_nth = 1
        self.comfy_srv = ThreadingHTTPServer(('127.0.0.1', 0), ComfyStub)
        threading.Thread(target=self.comfy_srv.serve_forever, daemon=True).start()
        comfy_url = f'http://127.0.0.1:{self.comfy_srv.server_address[1]}'
        self.model_srv = ThreadingHTTPServer(('127.0.0.1', 0), DirectorStub)
        DirectorStub.mode = 'good'
        threading.Thread(target=self.model_srv.serve_forever, daemon=True).start()
        self.model_url = (f'http://127.0.0.1:{self.model_srv.server_address[1]}'
                          '/v1/chat/completions')

        self.saved = (pilot.PILOT_ROOT, web.COMFY, web.COMFY_OUTPUT, enhance.COMFY,
                      enhance.COMFY_ROOT, web.JOB_TIMEOUT, web.POLL_SECONDS, jobs_db.FILE,
                      stats.FILE, dict(config.CFG.values))
        pilot.PILOT_ROOT = self.root / 'pilot-runs'
        web.COMFY = enhance.COMFY = comfy_url
        web.COMFY_OUTPUT = self.comfy / 'output'
        enhance.COMFY_ROOT = self.comfy
        web.JOB_TIMEOUT = 120.0
        web.POLL_SECONDS = 0.02
        jobs_db.FILE = self.root / 'jobs.db'
        jobs_db.init()
        stats.FILE = self.root / 'stats.json'
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        config.CFG.values.update({'comfy_url': comfy_url, 'comfy_root': str(self.comfy),
                                  'eval_fixture_dir': str(self.fixtures),
                                  'director_url': self.model_url, 'director_model': '',
                                  'pilot_portrait_image': ''})
        readiness.forget()

    def tearDown(self):
        (pilot.PILOT_ROOT, web.COMFY, web.COMFY_OUTPUT, enhance.COMFY, enhance.COMFY_ROOT,
         web.JOB_TIMEOUT, web.POLL_SECONDS, jobs_db.FILE, stats.FILE, values) = self.saved
        config.CFG.values = values
        self.comfy_srv.shutdown()
        self.comfy_srv.server_close()
        self.model_srv.shutdown()
        self.model_srv.server_close()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # helpers ---------------------------------------------------------------

    def plan(self, pilot_id='t', extra=()):
        argv = ['plan', '--id', pilot_id, '--scenes', str(self.root / 'scenes.json'),
                '--fixture-dir', str(self.fixtures), '--seeds', '5,7', '--order-seed', '99',
                '--width', '64', '--height', '48', *extra]
        return pilot.main(argv)

    def manifest(self, pilot_id='t'):
        return pilot.load_manifest(pilot_id)

    def render(self, pilot_id='t', extra=()):
        return pilot.main(['render', '--id', pilot_id, *extra])
    def prompts(self, pilot_id='t'):
        self.assertEqual(pilot.main(['prompts', '--id', pilot_id]), 0)



class PlanTests(PilotCase):
    def test_twelve_slots_two_arms_and_deterministic_order(self):
        self.assertEqual(self.plan(), 0)
        m = self.manifest()
        self.assertEqual(len(m['slots']), 12)
        self.assertEqual({s['arm'] for s in m['slots'].values()}, set(pilot.ARMS))
        self.assertEqual({s['seed'] for s in m['slots'].values()}, {5, 7})
        self.assertEqual(m['order_seed'], 99)
        # every scene x seed x arm cell exactly once
        cells = {(s['scene'], s['seed'], s['arm']) for s in m['slots'].values()}
        self.assertEqual(len(cells), 12)
        # the same order_seed reproduces the same render order
        self.assertEqual(self.plan('t2'), 0)
        self.assertEqual(self.manifest('t2')['order'], m['order'])
        self.assertNotEqual(m['order'], sorted(m['order']), 'render order must not be slot order')

    def test_private_variant_and_portable_fallback(self):
        self.assertEqual(self.plan(), 0)
        m = self.manifest()
        face = next(s for s in m['scenes'] if s['id'] == 'face-test')
        self.assertEqual(face['resolved_refs'], ['pilot-photo'])   # pilot-absent missing
        self.assertIsNone(m['fixtures']['pilot-absent']['path'])
        self.assertEqual(len(m['slots']), 12)

    def test_private_variant_picked_up_when_supplied(self):
        config.CFG.values['pilot_portrait_image'] = str(self.fixtures / 'pilot-photo-test.png')
        self.assertEqual(self.plan(), 0)
        face = next(s for s in self.manifest()['scenes'] if s['id'] == 'face-test')
        self.assertEqual(face['resolved_refs'], ['pilot-photo', 'pilot-absent'])

    def test_refusals(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.plan(), 2)                     # manifest exists: immutable, refuse
        bad = json.loads(json.dumps(SCENES))
        bad['scenes'] = bad['scenes'][:2]
        (self.root / 'bad.json').write_text(json.dumps(bad))
        rc = pilot.main(['plan', '--id', 'x', '--scenes', str(self.root / 'bad.json'),
                         '--fixture-dir', str(self.fixtures)])
        self.assertEqual(rc, 2)
        rc = pilot.main(['plan', '--id', 'y', '--scenes', str(self.root / 'scenes.json'),
                         '--fixture-dir', str(self.fixtures), '--seeds', '1,2,3'])
        self.assertEqual(rc, 2)

    def test_manifest_immutable_except_slots(self):
        self.assertEqual(self.plan(), 0)
        m = self.manifest()
        m['slots']['act-test-s5-simple']['prompt'] = {'text': 'x'}
        pilot.save_manifest(m)                               # slots: allowed
        m = self.manifest()
        m['settings']['width'] = 999
        with self.assertRaises(pilot.PilotError):
            pilot.save_manifest(m)
        m = self.manifest()
        m['order'] = list(reversed(m['order']))
        with self.assertRaises(pilot.PilotError):
            pilot.save_manifest(m)


class PromptTests(PilotCase):
    def test_both_arms_recorded_and_resume_keeps_them(self):
        self.plan()
        self.assertEqual(pilot.main(['prompts', '--id', 't']), 0)
        m = self.manifest()
        for slot in m['slots'].values():
            p = slot['prompt']
            self.assertTrue(p)
            if slot['arm'] == 'simple':
                self.assertEqual(p['text'],
                                 next(s['idea'] for s in m['scenes'] if s['id'] == slot['scene']))
                self.assertEqual(p['wrapper'], 'none')
            else:
                self.assertIn('text', p)
                self.assertGreaterEqual(p['calls'], 1)
                self.assertEqual(p['final_failures'], [])
        # rerun: nothing regenerated (same prompt dicts, model stub untouched)
        self.assertEqual(pilot.main(['prompts', '--id', 't']), 0)
        self.assertEqual(self.manifest()['slots']['face-test-s5-director']['prompt']['text'],
                         m['slots']['face-test-s5-director']['prompt']['text'])

    def test_failed_director_call_is_a_recorded_row(self):
        self.plan()
        DirectorStub.mode = 'empty'
        self.assertEqual(pilot.main(['prompts', '--id', 't']), 1)
        DirectorStub.mode = 'good'
        m = self.manifest()
        b = [s['prompt'] for s in m['slots'].values() if s['arm'] == 'director']
        self.assertTrue(all('error' in p and p['calls'] >= 1 for p in b))
        self.assertTrue(all('text' in s['prompt'] for s in m['slots'].values()
                            if s['arm'] == 'simple'))        # arm A never needed the model
        # resume: redo one slot explicitly, others stay the recorded failures; only the calls
        # THIS run makes decide the exit code, and this run's call succeeded
        self.assertEqual(pilot.main(['prompts', '--id', 't',
                                     '--redo-slot', 'act-test-s5-director']), 0)
        m = self.manifest()
        self.assertIn('text', m['slots']['act-test-s5-director']['prompt'])
        self.assertIn('error', m['slots']['talk-test-s7-director']['prompt'])


class RenderTests(PilotCase):
    def test_refused_when_video_not_ready(self):
        self.plan()
        config.CFG.values['comfy_url'] = 'http://127.0.0.1:1'
        readiness.forget()
        self.assertEqual(self.render(), 2)
        self.assertEqual(len(ComfyStub.posts), 0)
        self.assertTrue(all(s['render'] is None for s in self.manifest()['slots'].values()))

    def test_all_twelve_one_at_a_time_with_split_times(self):
        self.plan()
        self.prompts()
        order = self.manifest()['order']
        self.assertEqual(self.render(), 0)
        m = self.manifest()
        # one submit per slot, in the recorded randomised order; nothing parallel
        self.assertEqual(len(ComfyStub.posts), 12)
        self.assertEqual(len(ComfyStub.uploads), 4)         # only the 4 face-test slots upload
        for slot_id in order:
            r = m['slots'][slot_id]['render']
            self.assertEqual(r['state'], 'finished')
            self.assertAlmostEqual(r['backend_seconds'], 4.2, places=1)   # stub stamped 4200 ms
            self.assertGreaterEqual(r['wall_seconds'], 0)   # real submit+poll wall, kept separate
            self.assertGreaterEqual(r['queue_wait_seconds'], 0)
        # resume runs touch nothing
        self.assertEqual(self.render(), 0)
        self.assertEqual(len(ComfyStub.posts), 12)

    def test_failed_slot_recorded_then_never_auto_resubmitted(self):
        self.plan()
        self.prompts()
        ComfyStub.mode = 'execfail'
        ComfyStub.fail_nth = 2
        self.assertEqual(self.render(), 1)
        posts_after_first_run = len(ComfyStub.posts)
        self.assertEqual(posts_after_first_run, 12)
        m = self.manifest()
        states = [m['slots'][s]['render']['state'] for s in m['order']]
        self.assertEqual(states[1], 'failed')
        self.assertIn('stub boom', m['slots'][m['order'][1]]['render']['error'])
        self.assertEqual(states.count('finished'), 11)
        # a rerun skips everything: failures are recorded, not auto-resubmitted...
        self.assertEqual(self.render(), 1)
        self.assertEqual(len(ComfyStub.posts), posts_after_first_run)
        # ...unless this exact slot is retried explicitly
        ComfyStub.mode = 'ok'
        failed_slot = m['order'][1]
        self.assertEqual(self.render(extra=['--retry-slot', failed_slot]), 0)
        self.assertEqual(len(ComfyStub.posts), posts_after_first_run + 1)
        self.assertEqual(self.manifest()['slots'][failed_slot]['render']['state'], 'finished')

    def test_unknown_slot_stops_the_run(self):
        self.plan()
        self.prompts()
        ComfyStub.mode = 'silent'
        web.JOB_TIMEOUT = 1.0
        self.assertEqual(self.render(), 3)
        self.assertEqual(len(ComfyStub.posts), 1)            # never piles up unknown work
        m = self.manifest()
        first = m['order'][0]
        self.assertEqual(m['slots'][first]['render']['state'], 'unknown')
        self.assertTrue(m['slots'][first]['render']['prompt_ids'])
        # a later resume refuses to move past the unknown slot
        self.assertEqual(self.render(), 3)
        self.assertEqual(len(ComfyStub.posts), 1)

    def test_slot_without_prompt_stays_unrendered(self):
        self.plan()
        DirectorStub.mode = 'empty'
        pilot.main(['prompts', '--id', 't'])                 # every arm B prompt failed
        DirectorStub.mode = 'good'
        self.assertEqual(self.render(extra=['--only', 'act-test-s5-director,act-test-s5-simple']),
                         1)
        m = self.manifest()
        self.assertIsNone(m['slots']['act-test-s5-director']['render'])   # no prompt: no render
        self.assertEqual(m['slots']['act-test-s5-simple']['render']['state'], 'finished')
        self.assertEqual(len(ComfyStub.posts), 1)


class BlindingTests(PilotCase):
    def test_clips_page_and_key_reveal_nothing_about_arms(self):
        self.plan()
        DirectorStub.mode = 'good'
        pilot.main(['prompts', '--id', 't'])
        self.assertEqual(self.render(), 0)
        self.assertEqual(pilot.main(['review-prepare', '--id', 't', '--seed', '4242']), 0)
        review = pilot.PILOT_ROOT / 't' / 'review'
        names = sorted(p.name for p in review.iterdir() if p.suffix == '.mp4')
        self.assertEqual(names, [f'clip-{i:02d}.mp4' for i in range(1, 13)])
        html = (review / 'index.html').read_text()
        for word in SECRETS:
            self.assertNotIn(word, html.lower(), f'{word!r} leaked into index.html')
            for name in names:
                self.assertNotIn(word, name, f'{word!r} leaked into a clip filename')
        # the key is at the run root, not in review/, and not served
        self.assertTrue((pilot.PILOT_ROOT / 't' / 'key.json').is_file())
        self.assertFalse((review / 'key.json').exists())
        # every clip: ffmpeg-level metadata scanned too (source tag stripped, arms identical)
        probes = []
        for name in names:
            out = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                                  str(review / name)], capture_output=True, text=True).stdout
            # the filename= line is the scan path (tmpdir + clip name), not clip metadata
            out = '\n'.join(l for l in out.splitlines() if not l.startswith('filename='))
            probes.append(out)
            low = out.lower()
            for word in SECRETS:
                self.assertNotIn(word, low, f'{word!r} leaked into ffprobe tags of {name}')
        self.assertEqual(probes[0], probes[5], 'all clips must share one identical container '
                                               'profile (same codec tags, no encoder string)')
        self.assertNotIn('lavf', (probes[0] + probes[5]).lower())
        # same --seed reproduces the numbering; it does not follow slot order
        key1 = json.loads((pilot.PILOT_ROOT / 't' / 'key.json').read_text())
        self.assertEqual(pilot.main(['review-prepare', '--id', 't', '--seed', '4242']), 0)
        key2 = json.loads((pilot.PILOT_ROOT / 't' / 'key.json').read_text())
        self.assertEqual(key1['clips'], key2['clips'])
        mapping = [key1['clips'][f'clip-{i:02d}.mp4']['slot'] for i in range(1, 13)]
        self.assertNotEqual(mapping, sorted(mapping))
        # arms are spread over the numbering: not first-6-then-6
        arms = [key1['clips'][f'clip-{i:02d}.mp4']['arm'] for i in range(1, 13)]
        self.assertNotEqual(arms, ['simple'] * 6 + ['director'] * 6)
        self.assertEqual(arms.count('simple'), 6)


class RatingsServerTests(PilotCase):
    def _run_with_key(self):
        self.plan()
        m = self.manifest()
        key = {'kind': 'freevideo-pilot-key', 'seed': 1, 'created': pilot._utc(), 'clips': {}}
        for i, slot_id in enumerate(m['order'], 1):
            key['clips'][f'clip-{i:02d}.mp4'] = {'slot': slot_id, 'scene': m['slots'][slot_id]['scene'],
                                                 'arm': m['slots'][slot_id]['arm'],
                                                 'seed': m['slots'][slot_id]['seed'], 'take': ''}
        (pilot.PILOT_ROOT / 't' / 'key.json').write_text(json.dumps(key))
        (pilot.PILOT_ROOT / 't' / 'review').mkdir(parents=True, exist_ok=True)
        (pilot.PILOT_ROOT / 't' / 'review' / 'index.html').write_text('<html>ok</html>')
        return key

    def _server(self):
        srv = pilot.review_server('t', '127.0.0.1', 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return f'http://127.0.0.1:{srv.server_address[1]}'

    def _post(self, base, obj):
        req = urllib.request.Request(base + '/api/ratings', data=json.dumps(obj).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f'POST {obj} -> {e.code}: {body}')
            return e.code, body
    def test_append_only_log_and_current_view(self):
        self._run_with_key()
        base = self._server()
        rec = {'clip': 'clip-01', 'scores': {c: 3 for c in pilot.CRITERIA},
               'verdict': 'accepted', 'notes': 'n1'}
        self.assertEqual(self._post(base, rec)[0], 200)
        rec2 = dict(rec, verdict='rejected', notes='changed my mind')
        self.assertEqual(self._post(base, rec2)[0], 200)
        ratings = pilot.load_ratings('t')
        self.assertEqual(len(ratings['log']), 2)             # append-only: both records kept
        json.loads((pilot.PILOT_ROOT / 't' / 'ratings.json').read_text())  # still valid JSON
        with urllib.request.urlopen(base + '/api/ratings') as r:
            doc = json.loads(r.read())
        self.assertEqual(doc['clips']['clip-01']['verdict'], 'rejected')  # current = last
        self.assertNotIn('arm', json.dumps(doc))             # the API never carries key data

    def test_invalid_posts_rejected_and_concurrency_safe(self):
        self._run_with_key()
        base = self._server()
        code, _ = self._post(base, {'clip': 'clip-99', 'scores': {}})
        self.assertEqual(code, 400)
        code, _ = self._post(base, {'clip': 'clip-01', 'scores': {'action': 9}})
        self.assertEqual(code, 400)
        code, _ = self._post(base, {'clip': 'clip-01', 'scores': {'made-up': 3}})
        self.assertEqual(code, 400)
        threads = [threading.Thread(target=self._post, args=(base, {
            'clip': f'clip-{i:02d}', 'scores': {'action': (i % 5) + 1},
            'verdict': None, 'notes': ''})) for i in range(1, 9)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(pilot.load_ratings('t')['log']), 8)
        json.loads((pilot.PILOT_ROOT / 't' / 'ratings.json').read_text())


class ReportTests(PilotCase):
    """The report is checked on a synthetic run with exact numbers, so the arithmetic itself is
    part of the contract: backend time and wall time stay separate columns, failed/unfinished
    slots carry their cost with zero accepted takes."""

    def _fabricate(self):
        self.plan()
        m = self.manifest()
        clips = {}
        for i, slot_id in enumerate(m['order'], 1):
            clip = f'clip-{i:02d}.mp4'
            slot = m['slots'][slot_id]
            clips[clip] = {'slot': slot_id, 'scene': slot['scene'], 'arm': slot['arm'],
                           'seed': slot['seed'], 'take': ''}
            slot['render'] = {'state': 'finished', 'job_id': 'j', 'prompt_ids': ['p'],
                              'submitted_at': 0, 'started_at': 0, 'ended_at': 10,
                              'backend_seconds': 10.0, 'queue_wait_seconds': 1.0,
                              'hold_seconds': 0.1, 'wall_seconds': 30.0, 'take': '', 'error': None,
                              'at': ''}
            slot['prompt'] = ({'arm': slot['arm'], 'text': SENTINEL, 'chars': 999}
                              if slot['arm'] == 'simple' else
                              {'arm': 'director', 'text': SENTINEL, 'chars': 999,
                               'seconds_spent': 20.0})
        pilot.save_manifest(m)
        (pilot.PILOT_ROOT / 't' / 'key.json').write_text(json.dumps(
            {'kind': 'freevideo-pilot-key', 'seed': 1, 'created': '', 'clips': clips}))
        return clips

    def _rate(self, clips, accepted_slots):
        for clip, info in clips.items():
            score = 2 if info['arm'] == 'simple' else 4
            verdict = 'accepted' if info['slot'] in accepted_slots else 'rejected'
            pilot.append_rating('t', {'clip': clip, 'scores': {c: score for c in pilot.CRITERIA},
                                      'verdict': verdict, 'notes': '', 'at': pilot._utc()})

    def test_refuses_until_every_clip_is_rated(self):
        clips = self._fabricate()
        rated = dict(list(clips.items())[:11])               # one clip unrated, one half-rated
        for clip, info in rated.items():
            scores = {c: 3 for c in pilot.CRITERIA}
            scores.pop('audio')
            pilot.append_rating('t', {'clip': clip, 'scores': scores,
                                      'verdict': 'accepted', 'notes': '', 'at': pilot._utc()})
        self.assertEqual(pilot.main(['report', '--id', 't']), 2)
        self.assertFalse((pilot.PILOT_ROOT / 't' / 'report.md').exists())
        self.assertEqual(pilot.main(['report', '--id', 't', '--partial']), 0)
        md = (pilot.PILOT_ROOT / 't' / 'report.md').read_text()
        self.assertIn('PARTIAL REPORT', md)

    def test_full_report_arithmetic_and_no_prompt_metrics(self):
        clips = self._fabricate()
        accepted = {info['slot'] for info in clips.values() if info['scene'] == 'act-test'
                    and info['arm'] == 'simple'}
        accepted |= {info['slot'] for info in clips.values() if info['scene'] == 'talk-test'
                     and info['arm'] == 'director'}
        self._rate(clips, accepted)
        self.assertEqual(pilot.main(['report', '--id', 't']), 0)
        doc = json.loads((pilot.PILOT_ROOT / 't' / 'report.json').read_text())
        md = (pilot.PILOT_ROOT / 't' / 'report.md').read_text()
        self.assertNotIn('PARTIAL', md)
        self.assertIn('NOT evidence of superior video quality', md)
        # arms: 6 slots, 2 accepted -> 0.333; backend 60/2=30; wall 180/2=90
        for arm in pilot.ARMS:
            a = doc['arms'][arm]
            self.assertEqual((a['rated'], a['accepted']), (6, 2))
            self.assertAlmostEqual(a['acceptance_rate'], 0.333, places=3)
            t = a['time']
            self.assertEqual(t['backend_seconds_sum'], 60.0)
            self.assertEqual(t['per_accepted_take_backend_seconds'], 30.0)
            self.assertEqual(t['wall_seconds_sum'], 180.0)
            self.assertEqual(t['per_accepted_take_wall_seconds'], 90.0)
            mean = a['criteria']['action']['mean']
            self.assertEqual(mean, 2.0 if arm == 'simple' else 4.0)
            self.assertEqual(a['criteria']['action']['n'], 6)
        self.assertEqual(doc['arms']['simple']['time']['prompt_seconds_sum'], 0.0)
        self.assertIsNone(doc['arms']['simple']['time']['per_accepted_take_wall_plus_prompt_seconds'])
        d = doc['arms']['director']['time']
        self.assertEqual(d['prompt_seconds_sum'], 120.0)
        self.assertEqual(d['per_accepted_take_wall_plus_prompt_seconds'], 150.0)
        # scenes keep per-arm counts visible: act-test 4 rated / 2 accepted
        a = doc['scenes']['act-test']
        self.assertEqual((a['slots'], a['rated'], a['accepted']), (4, 4, 2))
        # n and ranges are printed; no significance claims
        self.assertIn('n=6', md)
        self.assertNotIn('significant', md.lower())
        # no prompt text, length or token numbers anywhere in either artefact
        raw_json = (pilot.PILOT_ROOT / 't' / 'report.json').read_text()
        self.assertNotIn(SENTINEL, raw_json)
        self.assertNotIn(SENTINEL, md)
        for token in ('"chars"', '"tokens"', '"length"', 'prompt length'):
            self.assertNotIn(token, raw_json)
        self.assertNotIn('"text"', raw_json)

    def test_failed_slots_cost_time_with_zero_accepts(self):
        clips = self._fabricate()
        m = self.manifest()
        two = [info['slot'] for info in clips.values() if info['arm'] == 'director'][:2]
        for slot_id in two:
            m['slots'][slot_id]['render'] = {'state': 'failed', 'job_id': 'j',
                                             'prompt_ids': ['p'], 'submitted_at': 0,
                                             'started_at': None, 'ended_at': None,
                                             'backend_seconds': None, 'queue_wait_seconds': None,
                                             'hold_seconds': 0.1, 'wall_seconds': 25.0,
                                             'take': None, 'error': 'stub boom', 'at': ''}
            m['slots'][slot_id]['prompt']['seconds_spent'] = 15.0
        pilot.save_manifest(m)
        self._rate(clips, {info['slot'] for info in clips.values()
                           if info['arm'] == 'simple'})     # only simple accepted
        self.assertEqual(pilot.main(['report', '--id', 't']), 0)
        doc = json.loads((pilot.PILOT_ROOT / 't' / 'report.json').read_text())
        d = doc['arms']['director']
        self.assertEqual(d['failed_slots'], 2)
        self.assertEqual(d['accepted'], 0)
        self.assertIsNone(d['time']['per_accepted_take_wall_seconds'])
        self.assertEqual(d['time']['backend_seconds_sum'], 40.0)   # 4 with data, not 6
        self.assertEqual(d['time']['backend_n'], 4)
        self.assertEqual(d['time']['wall_seconds_sum'], 170.0)    # 4*30 + 2*25: spent either way
        self.assertEqual(d['time']['wall_n'], 6)


if __name__ == '__main__':
    unittest.main()
