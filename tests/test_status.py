"""Dependency status: scene_status() derives prompt / refs / settings / continuity / instructions
flags ON READ from the selected take's job snapshot, the current scene fields, current reference
hashes, the upstream selection and the global bible/notes -- and stores nothing: a GET leaves
project.json byte for byte. Legacy takes answer unknown_legacy instead of guessing; downstream
chain members stay informational while their own immediate source stands; outdated takes and
their records survive untouched.

    python3 -m unittest discover tests      (from the repo root)

No ComfyUI and no ffmpeg: snapshots are hand-written with write_job_snapshot, matching the shape
web.render_snapshot records. The GET test drives the real web.Handler on a spare port.
"""
import copy, hashlib, json, secrets, shutil, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project as projects
import stats
import web

PLAN = {'title': 'Status test', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'},
                   {'title': 'Three', 'seconds': 6, 'connect': 'continue', 'action': 'three acts'}]}


class Status(unittest.TestCase):
    def setUp(self):
        self.roots = []
        self.saved = (projects.ROOT, stats.FILE, web.JOBS, web.SCENE_JOB, web.BATCH)
        for table in (web.JOBS, web.SCENE_JOB, web.BATCH, web.RESERVES, web.PROGRESS):
            table.clear()
        self.fresh()

    def tearDown(self):
        (projects.ROOT, stats.FILE, web.JOBS, web.SCENE_JOB, web.BATCH) = self.saved
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    # ---------------------------------------------------------------- fixture

    def fresh(self):
        """A temp projects root with the baseline film: three scenes (One cut, Two and Three
        continuations), each with one recorded take whose snapshot matches the scene exactly."""
        root = Path(tempfile.mkdtemp(prefix='director-status-'))
        self.roots.append(root)
        projects.ROOT = root
        stats.FILE = root / 'stats.json'
        self.slug = projects.create('Status film', 'nothing', 30, '16:9',
                                    [('cat.png', b'AAA'), ('dog.jpg', b'BBB')])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        def tune(p):
            p['bible'], p['notes'] = 'the bible', 'the notes'
            p['loras'] = projects.clean_loras([{'name': 'style.safetensors', 'strength': 1.0}])
            for s in p['scenes']:
                s['prompt'] = {'One': 'alpha', 'Two': 'beta', 'Three': 'gamma'}[s['title']]
        projects.mutate(self.slug, tune)
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]
        self.tag = 0
        self.takes = {}                                     # scene position -> [(id, job, run)]
        tid0, jid0, run0 = self.record(0, 'alpha', seed=7, mode='ref2va')
        self.takes[0] = [(tid0, jid0, run0)]
        tid1, jid1, run1 = self.record(1, 'beta', seed=8, mode='i2va', first=b'FRAME1',
                                       source={'scene': self.ids[0], 'take': tid0, 'run': run0})
        self.takes[1] = [(tid1, jid1, run1)]
        self.record(2, 'gamma', seed=9, mode='i2va', first=b'FRAME2',
                    source={'scene': self.ids[1], 'take': tid1, 'run': run1})

    def record(self, i, prompt, seed, mode='ref2va', first=None, source=None):
        """Write a jobs/<id>.json snapshot + take record the way render_job does, for scene position i."""
        self.tag += 1
        p = projects.load(self.slug)
        scene = p['scenes'][i]
        tid, jid = secrets.token_hex(8), secrets.token_hex(8)
        run = f'/comfy/output/director/run-{self.tag}-{i}'
        refs = []
        if first is None:                                   # non-continuation renders submit the ref pictures
            for n, name in enumerate(p['refs']):
                refs.append(dict(projects.store_ref(self.slug, name,
                                                    projects.path(self.slug, 'refs', name).read_bytes()), index=n))
        firsts = dict(projects.store_ref(self.slug, 'frame.png', first), source=source) if first else None
        projects.write_job_snapshot(self.slug, {
            'id': jid, 'kind': 'render-draft', 'created': time.time(), 'project': self.slug,
            'scene': str(scene['id']), 'take_id': tid, 'preset': 'draft', 'variant': None,
            'mode': mode, 'prompt': projects.ensure_i2va(prompt) if first else prompt,
            'seed': seed, 'width': p['size'][0], 'height': p['size'][1],
            'seconds': scene['seconds'], 'two_pass': False,
            'refs': refs, 'first': firsts, 'loras': p.get('loras'), 'graph': {},
            'director': {'model': None, 'skill_revision': None},
            'instructions': projects.instructions_of(p), 'finishing': None})
        src = dict(source, frame_sha256=firsts['sha256']) if firsts else None
        def apply(proj):
            s = proj['scenes'][i]
            s['seed'] = seed                                       # the scene seed the snapshot submitted
            s['takes'].append(
                {'id': tid, 'job': jid, 'provenance': 'recorded', 'run': run, 'path': run + '/video.mp4',
                 'url': None, 'preset': 'draft', 'seed': seed, 'created': time.time(), 'final': None,
                 'final_url': None, 'variant': None, 'source_take': src})
        projects.mutate(self.slug, apply)
        return tid, jid, run

    def add_legacy_take(self, i, run, source_take=None):
        """A pre-records take: no id, no job, no provenance. Returns its run (its selector ref)."""
        def apply(p):
            p['scenes'][i]['takes'].append(
                {'run': run, 'path': run + '/video.mp4', 'url': None, 'preset': 'draft', 'seed': 7,
                 'created': time.time(), 'final': None, 'final_url': None, 'variant': None,
                 'source_take': source_take})
        projects.mutate(self.slug, apply)
        return run

    # ---------------------------------------------------------------- assertions

    def check(self, case, expect, downstream=None):
        """expect: {position: {flag: state}} over the position-wise defaults; downstream:
        {position: [scene titles expected in downstream_of, in order]} overriding the chain default."""
        p = projects.load(self.slug)
        before = copy.deepcopy(p)
        json_bytes = projects.path(self.slug, 'project.json').read_bytes()
        def chain(pos):                                        # mirror of the forward chain, test-side
            out = []
            for q in p['scenes'][pos + 1:]:
                if q.get('connect') != 'continue':
                    break
                out.append(q['title'])
            return out
        for pos, s in enumerate(p['scenes']):
            st = projects.scene_status(p, s)
            want = {'prompt_changed': 'same', 'refs_changed': 'same', 'settings_changed': 'same',
                    'instructions_changed': 'same', 'finishing_changed': 'not_applicable',
                    'continuity_outdated': 'same' if (s.get('connect') == 'continue' and pos) else 'not_applicable'}
            want.update(expect.get(pos, {}))
            got = {}
            for name in projects.STATUS_FLAGS:
                flag = st['flags'][name]
                self.assertEqual(set(flag), {'state', 'reason', 'text'}, f'{case}: {name} flag shape')
                got[name] = flag['state']
            self.assertEqual(got, want, f'{case}: scene "{s["title"]}" flags')
            self.assertEqual(st['outdated'],
                             [k for k in projects.STATUS_FLAGS if want[k] == 'changed'], f'{case}: outdated')
            self.assertEqual(st['unknown_legacy'],
                             [k for k in projects.STATUS_FLAGS if want[k] == 'unknown_legacy'], f'{case}: unknown')
            want_down = (downstream or {}).get(pos, chain(pos))
            self.assertEqual([e['title'] for e in st['downstream_of']], want_down, f'{case}: downstream of "{s["title"]}"')
        # scene_status is pure and stores nothing: not in memory, not on disk
        self.assertEqual(p, before, f'{case}: scene_status mutated the project dict')
        self.assertEqual(projects.path(self.slug, 'project.json').read_bytes(), json_bytes,
                         f'{case}: status leaked into project.json')

    # ---------------------------------------------------------------- the table

    ALL_UNKNOWN = {'prompt_changed': 'unknown_legacy', 'refs_changed': 'unknown_legacy',
                   'settings_changed': 'unknown_legacy', 'instructions_changed': 'unknown_legacy'}

    def test_table(self):
        def set_prompt(self):
            projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('prompt', 'alpha two'))
        def set_mode(self):
            projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('prompt_mode', 't2va'))
        def set_mode_continue(self):
            projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('prompt_mode', 'ref2va'))
        def replace_ref(self):
            projects.path(self.slug, 'refs', 'picture-1.png').write_bytes(b'ZZZZ')
        def drop_ref(self):
            projects.mutate(self.slug, lambda p: p.__setitem__('refs', ['picture-2.jpg']))
        def seed_to_zero(self):
            projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('seed', 0))
        def set_seconds(self):
            projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('seconds', 10))
        def set_lora(self):
            projects.mutate(self.slug, lambda p: p.__setitem__(
                'loras', projects.clean_loras([{'name': 'style.safetensors', 'strength': 0.5}])))
        def pick_new_upstream(self):
            tid, _, run = self.record(0, 'alpha', seed=7, mode='ref2va')
            projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], tid))
        def auto_follows_new(self):
            self.record(0, 'alpha', seed=7, mode='ref2va')
        def reorder(self):
            def apply(p):
                projects.update_from_editor(p, {'scenes': [{'id': self.ids[k]} for k in (0, 2, 1)]})
            projects.mutate(self.slug, apply)
        def set_bible(self):
            projects.mutate(self.slug, lambda p: p.__setitem__('bible', 'the bible, revised'))
        def set_notes(self):
            projects.mutate(self.slug, lambda p: p.__setitem__('notes', 'the notes, revised'))
        def independent_scene(self):
            def apply(p):
                p['scenes'].append(projects.new_scene('Four'))
            projects.mutate(self.slug, apply)
            four = projects.load(self.slug)['scenes'][3]
            self.ids.append(four['id'])
            projects.mutate(self.slug, lambda p: p['scenes'][3].__setitem__('prompt', 'delta'))
            self.record(3, 'delta', seed=projects.load(self.slug)['scenes'][3]['seed'])
            set_prompt(self)
        def legacy_upstream(self):
            run = self.add_legacy_take(0, '/comfy/output/director/run-old')
            projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], run))
        def legacy_continue_with_source(self):
            def wipe(p):                                   # Two's take becomes a legacy record that kept its source
                s = p['scenes'][1]
                src = dict(s['takes'][0]['source_take'])
                s['takes'] = [{'run': '/comfy/output/director/run-old-2', 'path': 'x', 'url': None,
                               'preset': 'draft', 'seed': 8, 'created': time.time(), 'final': None,
                               'final_url': None, 'variant': None, 'source_take': src}]
            projects.mutate(self.slug, wipe)
        def snapshot_gone(self):
            projects.job_path(self.slug, self.takes[0][0][1]).unlink()

        U = self.ALL_UNKNOWN
        cases = [
            ('nothing changed', lambda s: None, {}, None),
            ('prompt edited', set_prompt, {0: {'prompt_changed': 'changed'}}, None),
            ('mode edited on a cut scene', set_mode, {0: {'prompt_changed': 'changed'}}, None),
            ('prompt_mode unset is not judged', set_mode_continue,
             {1: {'prompt_changed': 'same'}}, None),            # continue mode is forced by the frame
            ('reference content replaced', replace_ref, {0: {'refs_changed': 'changed'}}, None),
            ('reference removed', drop_ref, {0: {'refs_changed': 'changed'}}, None),
            ('seed rerolled to 0', seed_to_zero, {0: {'settings_changed': 'changed'}}, None),
            ('seconds changed', set_seconds, {0: {'settings_changed': 'changed'}}, None),
            ('lora strength changed', set_lora,
             {0: {'settings_changed': 'changed'}, 1: {'settings_changed': 'changed'},
              2: {'settings_changed': 'changed'}}, None),
            ('upstream selection changed', pick_new_upstream,
             {1: {'continuity_outdated': 'changed'}}, None),    # Three's immediate source stands
            ('auto fallback moved the source', auto_follows_new,
             {1: {'continuity_outdated': 'changed'}}, None),
            ('scenes reordered', reorder,
             {1: {'continuity_outdated': 'changed'}, 2: {'continuity_outdated': 'changed'}},
             {0: ['Three', 'Two'], 1: ['Two'], 2: []}),
            ('style bible changed', set_bible,
             {0: {'instructions_changed': 'changed'}, 1: {'instructions_changed': 'changed'},
              2: {'instructions_changed': 'changed'}}, None),
            ('director notes changed', set_notes,
             {0: {'instructions_changed': 'changed'}, 1: {'instructions_changed': 'changed'},
              2: {'instructions_changed': 'changed'}}, None),
            ('independent scene stays current', independent_scene,
             {0: {'prompt_changed': 'changed'}, 3: {}}, None),
            ('legacy take upstream answers unknown_legacy', legacy_upstream,
             {0: {**U}, 1: {'continuity_outdated': 'changed'}}, None),
            ('legacy continue take with a recorded source', legacy_continue_with_source,
             {1: {**U}, 2: {'continuity_outdated': 'changed'}}, None),  # own source kept; Three's is gone
            ('recorded take with its snapshot deleted', snapshot_gone, {0: {**U}}, None),
        ]
        for name, mutate, expect, downstream in cases:
            with self.subTest(case=name):
                self.fresh()
                mutate(self)
                self.check(name, expect, downstream)

    # ---------------------------------------------------------------- preservation

    def test_outdated_flags_delete_and_requeue_nothing(self):
        tid, _, run_new = self.record(0, 'alpha', seed=7, mode='ref2va')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], tid))
        runs = {i: [t['run'] for t in s['takes']]
                for i, s in enumerate(projects.load(self.slug)['scenes'])}
        p = projects.load(self.slug)
        for s in p['scenes']:
            projects.scene_status(p, s)
        after = {i: [t['run'] for t in s['takes']]
                 for i, s in enumerate(projects.load(self.slug)['scenes'])}
        self.assertEqual(runs, after)
        self.assertEqual(projects.chosen(projects.load(self.slug)['scenes'][0])['run'], run_new)
        # the superseded take's snapshot and result records are still on disk
        self.assertTrue(projects.job_path(self.slug, self.takes[0][0][1]).is_file())

    def test_continuity_reasons_name_the_drift(self):
        tid, _, run = self.record(0, 'alpha', seed=7, mode='ref2va')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], tid))
        p = projects.load(self.slug)
        st = projects.scene_status(p, p['scenes'][1])
        flag = st['flags']['continuity_outdated']
        self.assertEqual(flag['state'], 'changed')
        self.assertIn(f'source take {self.takes[0][0][0]} -> {tid}', flag['reason'])
        self.assertTrue(flag['text'])

    # ---------------------------------------------------------------- the wire

    def test_project_view_exposes_status_and_a_get_writes_nothing(self):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)   # the real handler, spare port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        f = projects.path(self.slug, 'project.json')
        before = f.read_bytes()
        def get():
            with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}/api/p/{self.slug}',
                                        timeout=10) as r:
                return json.loads(r.read())
        try:
            body = get()
            self.assertEqual(f.read_bytes(), before)              # a GET stores no derived status
            by_title = {s['title']: s for s in body['scenes']}
            st = by_title['Two']['status']
            self.assertEqual(st['provenance'], 'recorded')
            self.assertEqual({k: v['state'] for k, v in st['flags'].items()},
                             {'prompt_changed': 'same', 'refs_changed': 'same', 'settings_changed': 'same',
                              'continuity_outdated': 'same', 'instructions_changed': 'same',
                              'finishing_changed': 'not_applicable'})
            self.assertEqual([e['title'] for e in st['downstream_of']], ['Three'])
            self.assertEqual(by_title['One']['status']['take']['ref'], self.takes[0][0][0])
            self.assertEqual(by_title['One']['status']['outdated'], [])
            # ...and the view reflects a change computed live: edit the bible, GET again
            projects.mutate(self.slug, lambda p: p.__setitem__('bible', 'revised'))
            body = get()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual({s['title']: s['status']['flags']['instructions_changed']['state']
                          for s in body['scenes']},
                         {'One': 'changed', 'Two': 'changed', 'Three': 'changed'})

    def test_hash_cache_rehashes_only_when_the_file_moved(self):
        ref = projects.path(self.slug, 'refs', 'picture-1.png')
        sha = projects._file_sha256(ref)
        self.assertEqual(sha, hashlib.sha256(b'AAA').hexdigest())
        self.assertIs(projects._file_sha256(ref), sha)             # cache hit: same string object
        ref.write_bytes(b'ZZZZ')
        self.assertEqual(projects._file_sha256(ref), hashlib.sha256(b'ZZZZ').hexdigest())


if __name__ == '__main__':
    unittest.main()
