"""The take-poster endpoint (M3 Stage A): GET /api/p/<slug>/poster?id=<take ref> hands back a JPG
poster frame for one take, made with ffmpeg on demand and cached under the project's frames/
(gitignored, like the continuity frames). The take resolves exactly like the take endpoint (id or,
for pre-records takes, the run directory); the source video prefers the project's own media copy
(so a poster still works after the ComfyUI output folder has been pruned) and the enhanced file
before the draft one.

Mocked: ffmpeg in the stubbed cases (the command, the output path and the failure path are what
these tests pin). Real: one ffmpeg run against a real testsrc2 clip over the real web.Handler HTTP
server, proving the bytes cached under frames/ are an actual JPEG.

    python3 -m unittest discover tests      (from the repo root)
"""
import subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project as projects
import web

PLAN = {'title': 'Poster film', 'bible': 'b', 'music': '',
        'scenes': [{'title': 'One', 'seconds': 5, 'connect': 'cut', 'action': 'one acts'},
                   {'title': 'Two', 'seconds': 5, 'connect': 'cut', 'action': 'two acts'}]}


def take_rec(tid, slug, final=False):
    """A take record whose media copy lives in the project, whose run dir is elsewhere (or gone)."""
    url = f'/files/{slug}/media/{tid}.mp4'
    rec = {'id': tid, 'job': None, 'provenance': 'recorded', 'run': f'/gone/run-{tid}',
           'path': f'/gone/run-{tid}/video.mp4', 'url': url, 'preset': 'draft', 'seed': 7,
           'created': 0, 'final': None, 'final_url': None, 'variant': None, 'source_take': None}
    if final:
        rec['final'] = f'/gone/run-{tid}/final.mp4'
        rec['final_url'] = f'/files/{slug}/media/{tid}-final.mp4'
    return rec


class Poster(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='director-poster-'))
        projects.ROOT = cls.tmp / 'projects'
        slug = projects.create('Poster film', 'a hare runs', 10, '16:9', [])['slug']
        projects.mutate(slug, lambda p: projects.apply_storyboard(p, PLAN))
        cls.slug = slug
        ids = [s['id'] for s in projects.load(slug)['scenes']]
        cls.tid, cls.tid_final, cls.legacy_run = 'take-a', 'take-b', '/comfy/output/run-legacy'

        def add(p):
            p['scenes'][0]['takes'].append(take_rec(cls.tid, slug))
            p['scenes'][1]['takes'].append(take_rec(cls.tid_final, slug, final=True))
            p['scenes'][1]['takes'].append(          # pre-records take: addressed by run dir
                {'run': cls.legacy_run, 'path': f'{cls.legacy_run}/video.mp4',
                 'url': f'/files/{slug}/media/legacy.mp4', 'preset': 'draft', 'seed': 7,
                 'created': 0, 'final': None, 'final_url': None, 'variant': None, 'source_take': None})
        projects.mutate(slug, add)
        media = projects.path(slug, 'media')
        media.mkdir(parents=True)
        for name in ('take-a.mp4', 'take-b.mp4', 'take-b-final.mp4', 'legacy.mp4'):
            (media / name).write_bytes(b'not really video')      # stub cases: existence is enough

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.calls = []
        self.ffmpeg_fail = False
        def fake_run(cmd, *a, **k):
            self.calls.append(cmd)
            if self.ffmpeg_fail:
                return SimpleNamespace(returncode=1)
            Path(cmd[-1]).write_bytes(b'FAKEJPG')                # stand in for the extracted frame
            return SimpleNamespace(returncode=0)

        self._subprocess = web.subprocess
        web.subprocess = SimpleNamespace(run=fake_run)           # only the web module sees the fake

    def tearDown(self):
        web.subprocess = self._subprocess

    # ---------- resolution and source choice (stubbed ffmpeg) ----------
    def test_poster_is_cached_under_frames_and_made_from_the_media_copy(self):
        out = web.poster_view(self.slug, self.tid)
        self.assertEqual(out.parent.name, 'frames')
        self.assertEqual(out.name, 'take-a-poster.jpg')
        self.assertEqual(out.read_bytes(), b'FAKEJPG')
        cmd = self.calls[0]
        self.assertEqual(cmd[0], 'ffmpeg')
        self.assertIn(str(projects.path(self.slug, 'media', 'take-a.mp4')), cmd[cmd.index('-i') + 1])
        self.assertEqual(len(self.calls), 1)
        out2 = web.poster_view(self.slug, self.tid)              # second call: cached, no ffmpeg
        self.assertEqual(out2, out)
        self.assertEqual(len(self.calls), 1)

    def test_enhanced_take_posts_from_its_final_media(self):
        web.poster_view(self.slug, self.tid_final)
        cmd = self.calls[0]
        self.assertIn('take-b-final.mp4', cmd[cmd.index('-i') + 1])

    def test_legacy_take_is_addressed_by_its_run_dir(self):
        out = web.poster_view(self.slug, self.legacy_run)
        self.assertEqual(out.name, 'run-legacy-poster.jpg')      # path-safe name from the run dir
        cmd = self.calls[0]
        self.assertIn('legacy.mp4', cmd[cmd.index('-i') + 1])

    def test_unknown_take_is_a_project_error(self):
        with self.assertRaises(projects.ProjectError):
            web.poster_view(self.slug, 'nope')

    def test_missing_video_is_a_project_error(self):
        tid = 'take-c'
        projects.mutate(self.slug, lambda p: p['scenes'][0]['takes'].append(
            dict(take_rec(tid, self.slug), url=f'/files/{self.slug}/media/{tid}.mp4')))
        with self.assertRaises(projects.ProjectError) as cm:
            web.poster_view(self.slug, tid)
        self.assertIn('video unavailable', str(cm.exception))

    def test_ffmpeg_failure_raises_runtime_not_project_error(self):
        self.ffmpeg_fail = True
        with self.assertRaises(RuntimeError):
            web.poster_view(self.slug, self.tid)

    # ---------- one real ffmpeg over the real HTTP handler ----------
    def test_real_ffmpeg_over_http_serves_a_jpeg_from_the_cache_dir(self):
        clip = self.tmp / 'real.mp4'
        web.subprocess = self._subprocess                              # undo setUp's fake
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'testsrc2=size=160x90:rate=12:duration=2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)], check=True)
        tid = 'take-real'
        projects.mutate(self.slug, lambda p: p['scenes'][0]['takes'].append(take_rec(tid, self.slug)))
        shutil_copy = Path(projects.path(self.slug, 'media', f'{tid}.mp4'))
        shutil_copy.write_bytes(clip.read_bytes())

        app = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        try:
            port = app.server_address[1]
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/p/{self.slug}/poster?id={tid}',
                                        timeout=30) as r:
                body, kind = r.read(), r.headers.get('Content-Type')
            self.assertEqual(kind, 'image/jpeg')
            self.assertEqual(body[:2], b'\xff\xd8')              # a real JPEG, not a stub
            poster = projects.path(self.slug, 'frames', f'{tid}-poster.jpg')
            self.assertTrue(poster.is_file() and poster.stat().st_size > 0)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/p/{self.slug}/poster?id=ghost',
                                       timeout=10)
            self.assertEqual(cm.exception.code, 404)
        finally:
            app.shutdown()


if __name__ == '__main__':
    unittest.main()
