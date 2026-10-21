"""Cut fingerprints: every assembled cut stores a sha256 over the canonical JSON plan of what went
into it (ordered takes with their final/draft choice, transitions, cards, music, export settings);
the read view derives 'outdated' + 'changes' per output without storing anything, legacy cuts
without a fingerprint report 'unknown_legacy', and a GET leaves project.json byte for byte.

    python3 -m unittest discover tests      (from the repo root)

ComfyUI is not needed; assemble() runs against the fake ffmpeg of tests/test_selection.py.
"""
import itertools, json, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project as projects
import web


PLAN = {'title': 'Cut test', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'cut', 'action': 'two acts'},
                   {'title': 'Three', 'seconds': 6, 'connect': 'cut', 'action': 'three acts'}]}


class FakeFfmpeg:
    """Stands in for the subprocess module inside project.py: ffprobe answers a 6 s silent clip
    and the encode always succeeds, without ffmpeg ever running."""

    def run(self, cmd, **kw):
        stdout = '6.000000\n' if 'format=duration' in cmd else ''
        return subprocess.CompletedProcess(cmd, 0, stdout, '')


class Cuts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-cut-'))
        self.saved_root = projects.ROOT
        projects.ROOT = self.root
        for table in (web.JOBS, web.RESERVES, web.SCENE_JOB, web.BATCH, web.PROGRESS):
            table.clear()
        self.seq = itertools.count(1)
        self.slug = projects.create('Cut film', 'nothing', 30, '16:9', [])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]
        for i in range(3):
            self.add_take(i)

    def tearDown(self):
        projects.ROOT = self.saved_root
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def add_take(self, scene_ix=0, tid=None, final=None):
        run = f'/comfy/output/director/run-{next(self.seq)}'
        rec = {'run': run, 'path': run + '/video.mp4', 'url': '/files/x', 'preset': 'draft',
               'seed': 7, 'created': time.time(), 'final': final, 'final_url': None, 'variant': None}
        if tid:
            rec.update({'id': tid, 'job': tid, 'provenance': 'recorded'})
        projects.mutate(self.slug, lambda p: p['scenes'][scene_ix]['takes'].append(rec))
        return rec

    def set_music(self, blob=b'song-a'):
        projects.path(self.slug, 'music.mp3').write_bytes(blob)
        projects.mutate(self.slug, lambda p: p.__setitem__('music_file', 'music.mp3'))

    def assemble(self, kind='film'):
        """assemble() with a fake ffmpeg, stored into outputs exactly as web.batch_assemble does."""
        saved = projects.subprocess
        projects.subprocess = FakeFfmpeg()
        try:
            out = projects.assemble(projects.load(self.slug), kind)
        finally:
            projects.subprocess = saved
        projects.mutate(self.slug, lambda p: p['outputs'].__setitem__(kind, out))
        return out

    def view(self):
        return web.project_view(self.slug)['outputs']

    def stored(self, kind='film'):
        return projects.load(self.slug)['outputs'][kind]

    # ------------------------------------------------------------- the fingerprint

    def test_each_component_change_moves_the_fingerprint(self):
        p = projects.load(self.slug)
        base, base_a = projects.current_cut_fingerprint(p, 'film'), projects.current_cut_fingerprint(p, 'animatic')
        self.assertEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))  # stable
        self.add_take(0, tid='aaaaaaaabbbbcccc')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'aaaaaaaabbbbcccc'))  # takes
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('transition', 'crossfade'))  # transitions
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        projects.mutate(self.slug, lambda p: p.__setitem__('title_card', {'text': 'Cut film', 'seconds': 2}))  # cards
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        self.set_music()
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        projects.path(self.slug, 'music.mp3').write_bytes(b'song-b')      # replaced bed, same name
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        projects.mutate(self.slug, lambda p: p.__setitem__('aspect', '9:16'))  # export: film size
        self.assertNotEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))
        projects.mutate(self.slug, lambda p: p.__setitem__('size', [960, 540]))  # export: animatic size
        self.assertNotEqual(base_a, projects.current_cut_fingerprint(projects.load(self.slug), 'animatic'))

    def test_irrelevant_changes_keep_the_fingerprint(self):
        self.set_music()
        self.add_take(0, tid='aaaaaaaabbbbcccc')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'aaaaaaaabbbbcccc'))
        base = projects.current_cut_fingerprint(projects.load(self.slug), 'film')
        projects.mutate(self.slug, lambda p: p['scenes'][2].__setitem__('prompt', 'a cat walks left'))
        projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('seed', 123456))
        projects.mutate(self.slug, lambda p: p.__setitem__('bible', 'a different bible'))
        self.add_take(0)                                                 # newer take, scene 0 pinned elsewhere
        self.assertEqual(base, projects.current_cut_fingerprint(projects.load(self.slug), 'film'))

    def test_assemble_stores_the_fingerprint_with_the_entry(self):
        self.set_music()
        self.add_take(1, tid='bbbbbbbbccccdddd', final='/comfy/output/director/run-bbbb/final.mp4')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[1], 'bbbbbbbbccccdddd'))
        out = self.assemble('film')
        p = projects.load(self.slug)
        self.assertEqual(out['fingerprint'], projects.current_cut_fingerprint(p, 'film'))
        self.assertEqual([t['file'] for t in out['cut_plan']['takes']], ['draft', 'final', 'draft'])
        self.assertEqual(out['cut_plan']['takes'][1]['take'], 'bbbbbbbbccccdddd')
        stored = self.stored('film')
        self.assertEqual(stored['fingerprint'], out['fingerprint'])
        self.assertEqual(stored['cut_plan'], out['cut_plan'])
        self.assertNotIn('outdated', stored)                             # derived keys stay out of the file

    # ----------------------------------------------------------------- the read view

    def test_music_change_flags_the_cut_nothing_else_does(self):
        self.set_music()
        self.add_take(0, tid='aaaaaaaabbbbcccc')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'aaaaaaaabbbbcccc'))
        self.assemble('film')
        self.assertFalse(self.view()['film']['outdated'])
        self.assertEqual(self.view()['film']['changes'], [])
        self.assertNotEqual(self.view()['film']['fingerprint'], 'unknown_legacy')
        projects.path(self.slug, 'music.mp3').write_bytes(b'song-b')     # the bed underneath the cut changed
        self.assertEqual(self.view()['film']['changes'], ['music'])
        self.assertTrue(self.view()['film']['outdated'])
        # unrelated prompt, another scene's take finishing, and an alternative for the pinned
        # scene all stay current: the music flag is the only one
        projects.mutate(self.slug, lambda p: p['scenes'][2].__setitem__('prompt', 'revised'))
        self.add_take(0)                                                 # for an explicitly selected scene
        self.assertEqual(self.view()['film']['changes'], ['music'])
        projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('transition', 'dip'))
        self.assertEqual(self.view()['film']['changes'], ['transitions', 'music'])   # named, stable order

    def test_new_take_under_auto_selection_moves_the_takes_component(self):
        self.assemble('film')
        self.assertFalse(self.view()['film']['outdated'])
        self.add_take(0)                                                 # the auto pick follows it
        self.assertEqual(self.view()['film']['changes'], ['takes'])

    def test_take_finishing_for_an_explicitly_selected_scene_keeps_the_cut_current(self):
        self.add_take(0, tid='aaaaaaaabbbbcccc')
        projects.mutate(self.slug, lambda p: projects.set_explicit(p, self.ids[0], 'aaaaaaaabbbbcccc'))
        self.assemble('film')
        self.add_take(0)                                                 # a variant lands afterwards
        self.assertFalse(self.view()['film']['outdated'])
        self.assertEqual(self.view()['film']['changes'], [])

    def test_enhancing_a_used_take_moves_the_final_draft_choice(self):
        self.assemble('film')
        self.assemble('animatic')
        self.assertFalse(self.view()['film']['outdated'])
        p = projects.load(self.slug)                                     # a finish of the used take lands
        run = p['scenes'][0]['takes'][-1]['run']
        projects.mutate(self.slug, lambda pr: pr['scenes'][0]['takes'][-1].__setitem__('final', run + '/final.mp4'))
        self.assertEqual(self.view()['film']['changes'], ['takes'])      # a new film cut would use the final
        self.assertFalse(self.view()['animatic']['outdated'])            # the animatic cuts drafts anyway

    def test_reorder_moves_the_takes_component(self):
        projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('transition', 'crossfade'))
        self.assemble('film')
        projects.mutate(self.slug, lambda p: p.__setitem__('scenes', [p['scenes'][1], p['scenes'][0], p['scenes'][2]]))
        changes = self.view()['film']['changes']
        self.assertIn('takes', changes)                                  # the ordered cut changed
        self.assertIn('transitions', changes)

    def test_music_brief_without_a_bed_is_not_a_change(self):
        self.assemble('film')
        projects.mutate(self.slug, lambda p: p.__setitem__('music_brief', 'slow piano'))
        self.assertFalse(self.view()['film']['outdated'])                # no bed in the cut: nothing moved

    # ------------------------------------------------------------- legacy + read-only

    def test_legacy_output_reports_unknown_legacy_and_get_writes_nothing(self):
        legacy = {'file': 'film-20150101-000000.mp4', 'seconds': 30.0, 'scenes': 3, 'not_final': [],
                  'used_takes': [{'scene': i, 'take': f'/comfy/output/director/run-{n}',
                                  'run': f'/comfy/output/director/run-{n}', 'selection': 'auto'}
                                 for i, n in enumerate(self.ids, 1)],
                  'created': 1420070400.0}
        projects.path(self.slug, legacy['file']).write_bytes(b'old cut')   # historical media
        projects.mutate(self.slug, lambda p: p['outputs'].__setitem__('film', legacy))
        before = projects.path(self.slug, 'project.json').read_bytes()
        out = self.view()['film']
        self.assertEqual(out['fingerprint'], 'unknown_legacy')
        self.assertFalse(out['outdated'])
        self.assertEqual(out['changes'], [])
        self.assertEqual(out['file'], legacy['file'])                      # still points at the old media
        self.assertEqual(projects.path(self.slug, 'project.json').read_bytes(), before)  # a GET stores nothing
        self.assertTrue(projects.path(self.slug, legacy['file']).is_file())

    def test_view_never_edits_stored_entries(self):
        self.set_music()
        out = self.assemble('film')
        projects.path(self.slug, 'music.mp3').write_bytes(b'song-b')
        self.assertTrue(self.view()['film']['outdated'])                   # flagged on read
        stored = self.stored('film')
        self.assertEqual(stored, out)                                      # the file is exactly what assemble wrote

    def test_reassembly_never_touches_the_old_entry_or_file(self):
        first = self.assemble('film')
        projects.path(self.slug, first['file']).write_bytes(b'first cut')  # pretend ffmpeg left media
        time.sleep(1.05)                                                   # output names carry a second stamp
        self.set_music(b'song-b')
        second = self.assemble('film')
        self.assertNotEqual(first['file'], second['file'])
        self.assertEqual(projects.path(self.slug, first['file']).read_bytes(), b'first cut')
        self.assertEqual(self.view()['film']['changes'], [])               # the new cut is current


if __name__ == '__main__':
    unittest.main()
