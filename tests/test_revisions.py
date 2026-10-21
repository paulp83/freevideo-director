"""Project revisions: every write bumps `rev`, and an editor save with `base_rev` merges only when the
fields it touches are untouched since that rev.

    python3 -m unittest discover tests      (from the repo root)
"""
import shutil, sys, tempfile, threading, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project as projects

PLAN = {'title': 'Planned film', 'bible': 'the bible', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 6, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 6, 'connect': 'continue', 'action': 'two acts'}]}


def all_scenes(project):
    """The editor sends every scene it has; so do these tests, or they would be deleting scenes."""
    return [{'id': s['id']} for s in project['scenes']]


class Revisions(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='director-tests-'))
        self.real_root, projects.ROOT = projects.ROOT, self.root
        self.slug = projects.create('Rev test', 'a film about nothing', 30, '16:9', [])['slug']
        projects.mutate(self.slug, lambda p: projects.apply_storyboard(p, PLAN))
        self.ids = [s['id'] for s in projects.load(self.slug)['scenes']]

    def tearDown(self):
        projects.ROOT = self.real_root
        shutil.rmtree(self.root, ignore_errors=True)

    def rev(self):
        return projects.rev_of(projects.load(self.slug))

    def record_take(self, scene_id, run='director/run-1'):
        """What a finished render writes: one take on a scene and nothing else."""
        def apply(p):
            s = p['scenes'][projects.scene_index(p, scene_id)]
            s['takes'].append({'run': run, 'path': f'{run}/video.mp4', 'url': f'/files/{run}/video.mp4',
                               'preset': 'draft', 'seed': 7, 'created': 0.0, 'final': None, 'final_url': None,
                               'variant': None})
        return projects.mutate(self.slug, apply)

    # ---------------------------------------------------------------- rev bookkeeping

    def test_created_project_has_no_rev_until_the_first_write(self):
        bare = projects.create('No rev', 'untouched', 30, '16:9', [])
        self.assertNotIn('rev', bare)
        self.assertEqual(projects.rev_of(bare), 0)

    def test_rev_increments_on_every_write(self):
        seen = [self.rev()]
        self.assertEqual(seen, [1])                       # create() saves once without a rev
        self.record_take(self.ids[0])
        seen.append(self.rev())
        projects.save_editor(self.slug, {'notes': 'an editor note'})          # no base_rev: legacy path
        seen.append(self.rev())
        projects.mutate(self.slug, lambda p: projects.reroll_seed(p, self.ids[0]))
        seen.append(self.rev())
        self.assertEqual(seen, [1, 2, 3, 4])
        self.assertEqual(projects.load(self.slug)['rev'], 4)

    def test_save_reports_the_new_rev_and_stores_it(self):
        saved, fields = projects.save_editor(self.slug, {'title': 'Edited title'}, self.rev())
        self.assertEqual(fields, [])
        self.assertEqual(saved['rev'], 2)
        self.assertEqual(saved['title'], 'Edited title')
        stored = projects.load(self.slug)
        self.assertEqual(stored['rev'], 2)
        self.assertEqual(stored['key_revs']['title'], 2)

    def test_save_without_base_rev_behaves_as_before(self):
        self.record_take(self.ids[0])                     # the server moved on; the editor does not know
        saved, fields = projects.save_editor(self.slug, {'notes': 'kept working'})
        self.assertEqual(fields, [])
        self.assertEqual(saved['notes'], 'kept working')
        self.assertEqual(len(projects.load(self.slug)['scenes'][0]['takes']), 1)

    # ---------------------------------------------------------------- conflicts

    def test_stale_base_rev_on_a_changed_field_conflicts(self):
        base = self.rev()
        projects.mutate(self.slug, lambda p: p.__setitem__('title', 'renamed elsewhere'))
        self.record_take(self.ids[0])                     # also moves rev on
        saved, fields = projects.save_editor(self.slug, {'title': 'my title'}, base)
        self.assertIsNone(saved)
        self.assertEqual(fields, ['title'])
        self.assertEqual(projects.load(self.slug)['title'], 'renamed elsewhere')   # nothing was written
        self.assertEqual(self.rev(), 3)

    def test_non_conflicting_patch_merges(self):
        base = self.rev()
        self.record_take(self.ids[1])                     # a Draft lands on scene 2 while the editor types
        patch = {'scenes': [{'id': self.ids[0], 'prompt': 'the prompt I typed'},
                            {'id': self.ids[1]}]}
        saved, fields = projects.save_editor(self.slug, patch, base)
        self.assertEqual(fields, [])
        scenes = projects.load(self.slug)['scenes']
        self.assertEqual(scenes[0]['prompt'], 'the prompt I typed')
        self.assertEqual([t['run'] for t in scenes[1]['takes']], ['director/run-1'])
        self.assertEqual(saved['rev'], projects.load(self.slug)['rev'])   # the response carries the new rev
        self.assertGreater(saved['rev'], base)

    def test_director_prompt_write_conflicts_with_the_editors_prompt(self):
        base = self.rev()
        def director_writes(p):                            # what write_scene_prompt() leaves behind
            s = p['scenes'][projects.scene_index(p, self.ids[1])]
            s['prompt'], s['prompt_mode'], s['prompt_check'] = 'H3 from the model', 'direct', None
        projects.mutate(self.slug, director_writes)
        patch = {'scenes': [{'id': self.ids[0]}, {'id': self.ids[1], 'prompt': 'my own prompt'}]}
        saved, fields = projects.save_editor(self.slug, patch, base)
        self.assertIsNone(saved)
        self.assertEqual(fields, [f'scenes.{self.ids[1]}.prompt'])
        self.assertEqual(projects.load(self.slug)['scenes'][1]['prompt'], 'H3 from the model')

    def test_another_editors_prompt_on_another_scene_does_not_conflict(self):
        base = self.rev()
        def director_writes(p):
            s = p['scenes'][projects.scene_index(p, self.ids[0])]
            s['prompt'], s['prompt_mode'] = 'H3 from the model', 'direct'
        projects.mutate(self.slug, director_writes)
        patch = {'scenes': [{'id': self.ids[0], 'seconds': 9}, {'id': self.ids[1], 'prompt': 'mine'}]}
        saved, fields = projects.save_editor(self.slug, patch, base)
        self.assertEqual(fields, [])
        scenes = projects.load(self.slug)['scenes']
        self.assertEqual(scenes[0]['seconds'], 9)
        self.assertEqual(scenes[1]['prompt'], 'mine')
        self.assertEqual(scenes[0]['prompt'], 'H3 from the model')   # untouched by the merge

    def test_added_or_reordered_scenes_conflict(self):
        base = self.rev()
        projects.mutate(self.slug, lambda p: p['scenes'].insert(0, projects.new_scene('Added', 5)))
        saved, fields = projects.save_editor(self.slug, {'scenes': all_scenes(projects.load(self.slug))[:2]}, base)
        self.assertIsNone(saved)
        self.assertEqual(fields, ['scenes'])
        self.assertEqual(len(projects.load(self.slug)['scenes']), 3)

    def test_reordered_scenes_conflict(self):
        base = self.rev()
        projects.mutate(self.slug, lambda p: p['scenes'].reverse())
        saved, fields = projects.save_editor(self.slug, {'scenes': all_scenes(projects.load(self.slug))}, base)
        self.assertIsNone(saved)
        self.assertEqual(fields, ['scenes'])

    def test_takes_stored_on_a_scene_do_not_conflict_with_its_text_fields(self):
        base = self.rev()
        self.record_take(self.ids[0])
        projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('star', 'director/run-1'))
        patch = {'scenes': [{'id': self.ids[0], 'action': 'one acts again', 'transition': 'crossfade'},
                            {'id': self.ids[1]}]}
        saved, fields = projects.save_editor(self.slug, patch, base)
        self.assertEqual(fields, [])
        scene = projects.load(self.slug)['scenes'][0]
        self.assertEqual(scene['action'], 'one acts again')
        self.assertEqual(scene['star'], 'director/run-1')

    def test_server_writes_are_never_refused_and_bump_rev(self):
        projects.save_editor(self.slug, {'title': 'mine'}, self.rev())
        saved, fields = projects.save_editor(self.slug, {'title': 'stale'}, self.rev() - 1)     # refused
        self.assertIsNone(saved)
        self.assertTrue(fields)
        before = self.rev()
        self.record_take(self.ids[0])
        projects.mutate(self.slug, lambda p: projects.reroll_seed(p, self.ids[0]))
        projects.mutate(self.slug, lambda p: p['scenes'][0].__setitem__('star', 'director/run-9'))
        projects.mutate(self.slug, lambda p: p.__setitem__('last_batch', {'label': 'Drafting scenes', 'done': False}))
        self.assertEqual(self.rev(), before + 4)
        self.assertEqual(projects.load(self.slug)['title'], 'mine')

    def test_deleted_scenes_stop_being_tracked(self):
        projects.save_editor(self.slug, {'scenes': [{'id': self.ids[0], 'prompt': 'kept'},
                                                    {'id': self.ids[1], 'prompt': 'gone soon'}]}, self.rev())
        self.assertIn(f'scenes.{self.ids[1]}.prompt', projects.load(self.slug)['key_revs'])
        projects.save_editor(self.slug, {'scenes': [{'id': self.ids[0]}]}, self.rev())
        key_revs = projects.load(self.slug)['key_revs']
        self.assertNotIn(f'scenes.{self.ids[1]}.prompt', key_revs)
        self.assertIn(f'scenes.{self.ids[0]}.prompt', key_revs)

    def test_concurrent_saves_do_not_lose_each_other(self):
        base = self.rev()
        seen = []
        def save_a():
            seen.append(projects.save_editor(self.slug, {'notes': 'from tab A'}, base)[0])
        def save_b():
            seen.append(projects.save_editor(self.slug, {'title': 'from tab B'}, base)[0])
        threads = [threading.Thread(target=f) for f in (save_a, save_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stored = projects.load(self.slug)
        self.assertEqual(stored['rev'], base + 2)         # both saves landed, each bumping rev
        self.assertEqual(stored['notes'], 'from tab A')
        self.assertEqual(stored['title'], 'from tab B')
        self.assertTrue(all(p is not None for p in seen))

    # ---------------------------------------------------------------- delta saves

    def test_delta_save_keeps_takes_recorded_server_side(self):
        base = self.rev()
        self.record_take(self.ids[0])                    # a render finished while the user typed
        taken_rev = self.rev()
        saved, fields = projects.save_editor(self.slug, {'scenes': [{'id': self.ids[0], 'action': 'my new action'}],
                                                         'scenes_delta': True}, base)
        self.assertEqual(fields, [])                     # a take must not collide with an action edit
        self.assertEqual(saved['rev'], taken_rev + 1)
        stored = projects.load(self.slug)
        self.assertEqual(stored['scenes'][0]['action'], 'my new action')
        self.assertEqual(len(stored['scenes'][0]['takes']), 1, 'Keep mine must never drop a recorded take')

    def test_delta_only_touches_the_scenes_it_names(self):
        base = self.rev()
        projects.mutate(self.slug, lambda p: p['scenes'][1].__setitem__('action', 'rewritten on the server'))
        saved, fields = projects.save_editor(self.slug, {'scenes': [{'id': self.ids[0], 'seconds': 9}],
                                                         'scenes_delta': True}, base)
        self.assertEqual(fields, [], 'a clean scene the patch never mentions cannot conflict')
        stored = projects.load(self.slug)
        self.assertEqual(stored['scenes'][0]['seconds'], 9)
        self.assertEqual(stored['scenes'][1]['action'], 'rewritten on the server')

    def test_delta_naming_a_deleted_scene_conflicts(self):
        base = self.rev()
        projects.mutate(self.slug, lambda p: p['scenes'].pop())
        saved, fields = projects.save_editor(self.slug, {'scenes': [{'id': self.ids[1], 'action': 'mine'}],
                                                         'scenes_delta': True}, base)
        self.assertIsNone(saved)
        self.assertIn('scenes', fields)                  # refused; the page offers Reload

    def test_delta_applies_the_first_scene_rules_and_prompt_history(self):
        base = self.rev()
        saved, fields = projects.save_editor(self.slug, {'scenes': [
            {'id': self.ids[0], 'connect': 'continue', 'transition': 'crossfade', 'prompt': 'a first prompt'}],
            'scenes_delta': True}, base)
        self.assertEqual(fields, [])
        s = saved['scenes'][0]
        self.assertEqual(s['connect'], 'cut')            # scene one never connects or transitions
        self.assertEqual(s['transition'], 'cut')
        self.assertEqual(s['prompt'], 'a first prompt')
        self.assertTrue(s['history'], 'a delta prompt edit keeps history')


if __name__ == '__main__':
    unittest.main()
