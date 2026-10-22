"""Render-stage progress: FreeVideo's overall fraction wins while the prompt is the one ComfyUI runs, and the sampler
step counter is the fallback. Found against a real render: the step counter hit 50/50 forty seconds into a 110 s
render, and a reattached job saw no standard progress at all."""
import sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web


class RenderProgress(unittest.TestCase):
    def setUp(self):
        self.saved = (web.comfy_queue, dict(web.FV_PROGRESS), dict(web.PROGRESS))
        web.FV_PROGRESS.clear(); web.PROGRESS.clear()
        self.job = web.new_job('render-draft', ['render'])
        self.st = web.stage(self.job, 'render')

    def tearDown(self):
        web.comfy_queue = self.saved[0]
        web.FV_PROGRESS.clear(); web.FV_PROGRESS.update(self.saved[1])
        web.PROGRESS.clear(); web.PROGRESS.update(self.saved[2])
        web.JOBS.clear()

    def fv(self, fraction=0.67, age=0.0, label='Decoding the video'):
        web.FV_PROGRESS.update(fraction=fraction, label=label, remaining=12.0, at=time.time() - age)

    def test_the_overall_fraction_is_used_while_our_prompt_runs(self):
        web.comfy_queue = lambda: ({'p1'}, set())
        self.fv(0.67)
        web.PROGRESS['p1'] = {'node': '1', 'value': 50, 'max': 50}      # the sampler counter is already "done"
        web.render_progress(self.job, 'p1')
        self.assertEqual((self.st['value'], self.st['max'], self.st['unit']), (67, 100, '%'))
        self.assertEqual(self.st['detail'], 'Decoding the video')

    def test_a_neighbours_progress_is_not_ours(self):
        web.comfy_queue = lambda: ({'someone-else'}, {'p1'})              # ours is still pending
        self.fv(0.67)
        web.PROGRESS['p1'] = {'node': '1', 'value': 0, 'max': 0}
        web.render_progress(self.job, 'p1')
        self.assertEqual((self.st['value'], self.st['max']), (0, 0))

    def test_stale_progress_falls_back_to_the_sampler_counter(self):
        web.comfy_queue = lambda: ({'p1'}, set())
        self.fv(0.67, age=30)
        web.PROGRESS['p1'] = {'node': '1', 'value': 12, 'max': 50}
        web.render_progress(self.job, 'p1')
        self.assertEqual((self.st['value'], self.st['max']), (12, 50))
        self.assertNotIn('unit', self.st)

    def test_an_unreachable_queue_falls_back_without_guessing(self):
        web.comfy_queue = lambda: None
        self.fv(0.67)
        web.PROGRESS['p1'] = {'node': '1', 'value': 3, 'max': 50}
        web.render_progress(self.job, 'p1')
        self.assertEqual((self.st['value'], self.st['max']), (3, 50))

    def test_the_fraction_is_clamped_to_a_percentage(self):
        web.comfy_queue = lambda: ({'p1'}, set())
        self.fv(1.4)
        web.render_progress(self.job, 'p1')
        self.assertEqual(self.st['value'], 100)

    def test_the_public_job_carries_unit_and_detail_to_the_pages(self):
        web.comfy_queue = lambda: ({'p1'}, set())
        self.fv(0.5, label='Sampling')
        web.render_progress(self.job, 'p1')
        stage = web.public(self.job)['stages'][0]
        self.assertEqual((stage['value'], stage['unit'], stage['detail']), (50, '%', 'Sampling'))


if __name__ == '__main__':
    unittest.main()
