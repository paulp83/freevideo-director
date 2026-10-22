"""A render ComfyUI answers from its cache (same seed and inputs) returns the video an earlier take already points
at: it must not be recorded as a second take, and the job says why in plain words. Found against a real ComfyUI."""
import shutil, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project as projects
import web


class DuplicateTake(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dup-take-'))
        self.saved = projects.ROOT
        projects.ROOT = self.tmp / 'projects'
        p = projects.create('Dup', 'x', 8, '16:9', [])
        p['scenes'] = [projects.new_scene('One', 4)]
        projects.save(p)
        self.slug, self.sid = p['slug'], p['scenes'][0]['id']

    def tearDown(self):
        projects.ROOT = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        web.JOBS.clear()

    def record(self, take_id, run):
        job = web.new_job('render-draft', ['render'], project=self.slug, scene=self.sid)
        web.take_recorder(self.slug, self.sid, job['id'], take_id, 'draft', 5, None, None)(
            {'run': run, 'path': f'{run}/video.mp4', 'url': f'/f/{run}'})
        return job

    def takes(self):
        return projects.load(self.slug)['scenes'][0]['takes']

    def test_a_new_run_is_recorded(self):
        job = self.record('t1', 'out/run-a')
        self.assertEqual([t['id'] for t in self.takes()], ['t1'])
        self.assertIsNone(job.get('notice'))

    def test_the_same_run_again_is_not_a_second_take(self):
        self.record('t1', 'out/run-a')
        job = self.record('t2', 'out/run-a')                 # ComfyUI's cache: identical request, identical file
        self.assertEqual([t['id'] for t in self.takes()], ['t1'])
        self.assertIn('cached result', job['notice'])
        self.assertIn('t1', job['notice'])                   # names the take that already holds the video
        self.assertEqual(web.public(job)['notice'], job['notice'])

    def test_a_different_run_after_a_duplicate_is_still_recorded(self):
        self.record('t1', 'out/run-a')
        self.record('t2', 'out/run-a')
        self.record('t3', 'out/run-b')
        self.assertEqual([t['id'] for t in self.takes()], ['t1', 't3'])

    def test_replaying_the_same_job_stays_idempotent(self):
        job = self.record('t1', 'out/run-a')
        web.take_recorder(self.slug, self.sid, job['id'], 't1', 'draft', 5, None, None)(
            {'run': 'out/run-a', 'path': 'out/run-a/video.mp4', 'url': '/f/out/run-a'})
        self.assertEqual(len(self.takes()), 1)
        self.assertIsNone(job.get('notice'))                 # a replay of the same job is not a duplicate


class ResultFile(unittest.TestCase):
    def test_a_stage_open_when_the_result_is_written_gets_an_end_time(self):
        # found on real renders: every result file said the render stage never ended
        tmp = Path(tempfile.mkdtemp(prefix='result-stage-'))
        saved = projects.ROOT
        projects.ROOT = tmp / 'projects'
        try:
            p = projects.create('Res', 'x', 8, '16:9', [])
            slug = p['slug']
            job = web.new_job('render-draft', ['render'], project=slug, scene='s1')
            web.activate(job, 'render')                      # the render stage is active when the job body ends
            job['take'] = {'run': 'out/r', 'path': 'out/r/video.mp4', 'url': '/f'}
            web.write_job_result(job, 'take-1')
            res = projects.read_job_result(slug, job['id']) if hasattr(projects, 'read_job_result') else \
                __import__('json').loads((projects.ROOT / slug / 'jobs' / f"{job['id']}.result.json").read_text())
            stage = res['stages'][0]
            self.assertIsNotNone(stage['ended'])
            self.assertGreaterEqual(stage['ended'], stage['started'])
        finally:
            projects.ROOT = saved
            shutil.rmtree(tmp, ignore_errors=True)
            web.JOBS.clear()


if __name__ == '__main__':
    unittest.main()
