#!/usr/bin/env python3
"""M5 finishing acceptance: real ffmpeg/ffprobe end-to-end, plus legacy (M4) golden parity.

Four suites, no GPU and no real ComfyUI:

* AcceptanceExport       -- assemble() a mixed-rate fixture film with the real ffmpeg and
                            measure the exported streams (fps policy, A/V durations, a 1 kHz
                            beep located in the decoded PCM, container length).
* SwitchSeparation       -- the finishing switches are separate knobs: encode-only vs the
                            full chain, crf changes only encode args, fps_policy alone
                            changes only the 'export' component of the cut plan.
* LegacyGoldenParity     -- the M4-era project (tests/fixtures/legacy_m4.py) still loads,
                            reads are side-effect free, and the values pinned in
                            tests/golden/legacy_m4_*.json still come out byte-equal.
* WebFinishingGate       -- over the real Handler HTTP stack (web.py subprocess + the
                            tests/stub_comfy.py ComfyUI + an in-file director stub): which
                            actions a missing finishing dependency blocks (503) and which
                            stay open (prompt/draft render), and that a project's own
                            switches excuse the dependency of the stage they switched off.

Skipped entirely when ffmpeg/ffprobe are not installed. Everything lives under one temp
root (removed in tearDownModule); the repo's projects/, jobs.db, stats.json and
config.json are never touched.
"""
import array
import http.server
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
HAS_FFMPEG = bool(shutil.which('ffmpeg')) and bool(shutil.which('ffprobe'))

MODROOT = Path(tempfile.mkdtemp(prefix='director-m5-accept-'))
_inproc = MODROOT / 'inproc'
_inproc.mkdir()
_env_saved = os.environ.get('PROJECTS_ROOT')
os.environ['PROJECTS_ROOT'] = str(_inproc)

sys.path.insert(0, str(HERE / 'golden'))
sys.path.insert(0, str(HERE / 'fixtures'))
import capture_legacy_m4  # noqa: E402
import legacy_m4  # noqa: E402

import enhance  # noqa: E402
import finishing  # noqa: E402
import project as projects  # noqa: E402
import web  # noqa: E402

# project.py snapshots projects_root at import time; during a full-suite run an
# earlier test module may have imported it already. The in-process classes below
# re-point ROOT at the temp dir in their setUpClass (never at import time: that
# would poison test_config, which checks the real module globals), and the
# original value is restored in tearDownModule.
_ROOT_ORIG = projects.ROOT

GOLD = HERE / 'golden'


def tearDownModule():
    if _env_saved is None:
        os.environ.pop('PROJECTS_ROOT', None)
    else:
        os.environ['PROJECTS_ROOT'] = _env_saved
    projects.ROOT = _ROOT_ORIG
    shutil.rmtree(MODROOT, ignore_errors=True)


# ---------------------------------------------------------------- acceptance fixture

ACC_SCENES = [  # id, title, transition, connect
    ('11aa11', 'Kite', 'cut', 'cut'),
    ('22bb22', 'Field', 'crossfade', 'continue'),
    ('33cc33', 'Dusk', 'dip', 'continue'),
]
# Title card 1.5 s + 3 x 4.0 s clips - 0.5 s crossfade + end card 1.0 s:
EXPECTED_FILM_SECONDS = 1.5 + 4.0 + (4.0 - 0.5) + 4.0 + 1.0
BEEP_TIMELINE_AT = 1.5 + 4.0 - 0.5 + 1.0  # beep sits at t=1.0 inside scene 2's clip


def install_acc(root, slug='finishing-acc'):
    """Modern (M5) acceptance project: cards on, ids in take records, mixed-rate takes.

    Scenes 1 and 3 have a 48 fps final.mkv (an enhanced take) plus the draft take.mkv;
    scene 2 has only its draft take -- so the film proves the fps normalisation
    (48 + 24 + 48 sources through one fps filter) and reports the non-final scene.
    Scene 2's take carries a 1 kHz beep gated to its [1.0, 2.0] window."""
    root = Path(root)
    scenes = []
    for i, (sid, title, transition, connect) in enumerate(ACC_SCENES, 1):
        run = f'20250301-00000{i}-acc'
        rundir = root / slug / 'scenes' / f'{i:02d}' / run
        rundir.mkdir(parents=True, exist_ok=True)
        if i == 2:
            legacy_m4.make_take(rundir / 'take.mkv', 4.0, 24, beep=(1.0, 2.0, 1000.0))
            final = None
        else:
            legacy_m4.make_take(rundir / 'take.mkv', 4.0, 24, tone=220.0 * i)
            legacy_m4.make_take(rundir / 'final.mkv', 4.0, 48, tone=220.0 * i)
            final = str(rundir / 'final.mkv')
        legacy_m4.make_take(rundir / 'video.mp4', 1.0, 24, tone=220.0 * i)
        (rundir / 'video.artifacts').mkdir(exist_ok=True)
        scenes.append({
            'id': sid, 'title': title, 'seconds': 4, 'connect': connect, 'action': '',
            'prompt': f'At 00:01.0, [Shot 1] the {title.lower()} moves under a low sky.',
            'prompt_mode': 't2va', 'seed': 3000 + i,
            'takes': [{'id': f'act{i}', 'run': str(rundir), 'path': str(rundir / 'take.mkv'),
                       'url': f'/files/{slug}/media/{run}.mp4', 'preset': 'draft',
                       'seed': 4000 + i, 'created': 1740800000.0 + i, 'final': final,
                       'final_url': None, 'variant': None, 'source_take': None,
                       'thumb_url': None}],
            'star': None, 'selected_take': None, 'selection': 'auto',
            'transition': transition, 'history': [], 'prompt_check': None,
        })
    project = {'slug': slug, 'name': 'Finishing Acceptance',
               'logline': 'A field guide to wind.', 'seconds': 12, 'aspect': '16:9',
               'size': [640, 360], 'refs': [], 'music_file': None,
               'title': 'Finishing Acceptance', 'bible': 'Wind crosses a dusk field.',
               'music_brief': '', 'notes': '',
               'title_card': {'text': 'A Field Guide to Wind', 'seconds': 1.5},
               'end_card': {'text': 'The End', 'seconds': 1.0},
               'scenes': scenes, 'outputs': {}, 'characters': [], 'character_notes': '',
               'character_refs': {}, 'created': 1740787200.0}
    pdir = root / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / 'project.json').write_text(json.dumps(project, indent=1))
    return project


def set_finishing(slug, settings):
    canonical = finishing.clean_finishing(settings)
    projects.mutate(slug, lambda p: projects.update_from_editor(p, {'finishing': canonical}))
    return canonical


def ffprobe_json(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json',
                          '-show_format', '-show_streams', str(path)],
                         check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def streams(probe, codec):
    return [s for s in probe['streams'] if s['codec_type'] == codec]


def first_tone_onset(path, freq, lo, hi):
    """Locate the onset of a pure `freq` Hz tone (the fixture beep is 1 kHz; the scenes'
    base tones are 220/440/660 Hz). A 20 ms sliding one-bin DFT is stepped every 2.5 ms;
    the onset is the start of the first window whose tone magnitude reaches 25% of the
    peak in [lo, hi). Loudness alone cannot find it: loudnorm's dynamic gain lifts the
    quiet segments right at the scene joins."""
    out = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 's16le',
                          '-ac', '1', '-ar', '48000', '-'],
                         check=True, capture_output=True)
    s = array.array('h', out.stdout)
    fs, n, step = 48000, 960, 120
    w = complex(math.cos(-2 * math.pi * freq / fs), math.sin(-2 * math.pi * freq / fs))
    mags = []
    starts = list(range(int(lo * fs), min(int(hi * fs), len(s) - n), step))
    for a in starts:
        acc = 0j
        for j in range(a, a + n):
            acc = acc * w + s[j]
        mags.append((acc.real * acc.real + acc.imag * acc.imag))
    peak = max(mags)
    thr = peak * 0.0625          # magnitude peak^2 * 0.25^2
    for a, m in zip(starts, mags):
        if m >= thr:
            return a / fs
    raise AssertionError(f'no {freq:g} Hz onset in {path} between {lo} s and {hi} s')


def fake_assemble(project):
    """Run assemble() with the encode replaced by capture_legacy_m4's FakePopen: the
    exact ffmpeg command line is recorded, nothing is encoded. Cards (which have no
    '-movflags') really render, so the card probes see real files. Returns (result, cmd)."""
    capture_legacy_m4.FakePopen.calls.clear()

    def record(cmd, **kwargs):
        if kwargs.get('stdout') is subprocess.PIPE and '-filter_complex' in cmd \
                and '-movflags' in cmd:
            return capture_legacy_m4.FakePopen(cmd)
        return real(cmd, **kwargs)

    real = subprocess.Popen
    projects.subprocess.Popen = record
    try:
        res = projects.assemble(project, 'film')
    finally:
        projects.subprocess.Popen = real
    (cmd,) = capture_legacy_m4.FakePopen.calls
    return res, cmd


# ---------------------------------------------------------------- exports with real ffmpeg

@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not installed')
class AcceptanceExport(unittest.TestCase):
    slug = 'finishing-acc'

    @classmethod
    def setUpClass(cls):
        projects.ROOT = _inproc      # see _ROOT_ORIG note at the imports
        install_acc(projects.ROOT)
        cls.outs, cls.probes, cls.beeps = {}, {}, {}
        for policy in ('original', 'interpolated'):
            set_finishing(cls.slug, {'fps_policy': policy})
            p = projects.load(cls.slug)
            t0 = time.monotonic()
            res = projects.assemble(p, 'film')
            wall = time.monotonic() - t0
            cls.outs[policy] = (res, wall)
            film = Path(projects.path(cls.slug, res['file']))
            cls.probes[policy] = ffprobe_json(film)
            cls.beeps[policy] = first_tone_onset(film, 1000.0, 5.2, 7.2)
            fmt = cls.probes[policy]['format']
            v = streams(cls.probes[policy], 'video')[0]
            a = streams(cls.probes[policy], 'audio')[0]
            print(f'\n[export {policy}] file={film.name} wall={wall:.2f}s '
                  f'fps={v["avg_frame_rate"]} container={float(fmt["duration"]):.3f}s '
                  f'video={float(v.get("duration", fmt["duration"])):.3f}s '
                  f'audio={float(a.get("duration", fmt["duration"])):.3f}s '
                  f'beep_onset={cls.beeps[policy]:.3f}s (expected {BEEP_TIMELINE_AT:.3f}s)')

    def check_streams_and_length(self, policy, fps):
        probe = self.probes[policy]
        vs, as_ = streams(probe, 'video'), streams(probe, 'audio')
        self.assertEqual(len(vs), 1, 'exactly one video stream')
        self.assertEqual(len(as_), 1, 'exactly one audio stream')
        v, a = vs[0], as_[0]
        self.assertEqual(Fraction(v['avg_frame_rate']), Fraction(fps, 1))
        self.assertEqual(v['pix_fmt'], 'yuv420p')
        container = float(probe['format']['duration'])
        self.assertAlmostEqual(container, EXPECTED_FILM_SECONDS, delta=1 / fps,
                               msg='container length vs cut sum minus the xfade')
        nb = int(v.get('nb_frames') or round(container * fps))
        self.assertLessEqual(abs(nb - container * fps), 2, 'nb_frames matches duration*fps')

    def test_original_policy_exports_source_fps(self):
        self.check_streams_and_length('original', 24)

    def test_interpolated_policy_exports_doubled_fps(self):
        self.check_streams_and_length('interpolated', 48)

    def test_mixed_rate_sources_are_normalised(self):
        res = self.outs['interpolated'][0]
        self.assertEqual(res['not_final'], [2], 'scene 2 has no final take')
        self.assertEqual(len(res['used_takes']), 3)
        # the sources really are mixed rate: scene 1's final is 48 fps, scene 2's take 24
        s1 = streams(ffprobe_json(projects.ROOT / self.slug / 'scenes/01'
                                  / '20250301-000001-acc/final.mkv'), 'video')[0]
        s2 = streams(ffprobe_json(projects.ROOT / self.slug / 'scenes/02'
                                  / '20250301-000002-acc/take.mkv'), 'video')[0]
        self.assertEqual(Fraction(s1['avg_frame_rate']), Fraction(48, 1))
        self.assertEqual(Fraction(s2['avg_frame_rate']), Fraction(24, 1))
        for policy in ('original', 'interpolated'):
            v = streams(self.probes[policy], 'video')[0]
            want = finishing.output_fps(24, finishing.clean_finishing({'fps_policy': policy}))
            self.assertEqual(Fraction(v['avg_frame_rate']), Fraction(want, 1))

    def test_audio_and_video_streams_agree_within_one_frame(self):
        for policy, fps in (('original', 24), ('interpolated', 48)):
            probe = self.probes[policy]
            fmt = float(probe['format']['duration'])
            v = float(streams(probe, 'video')[0].get('duration', fmt))
            a = float(streams(probe, 'audio')[0].get('duration', fmt))
            delta = abs(v - a)
            print(f'[a/v {policy}] video={v:.3f}s audio={a:.3f}s delta={delta:.4f}s '
                  f'(one frame = {1 / fps:.4f}s)')
            self.assertLessEqual(delta, 1 / fps)

    def test_beep_sits_at_its_scene_offset_under_both_policies(self):
        for policy in ('original', 'interpolated'):
            self.assertAlmostEqual(self.beeps[policy], BEEP_TIMELINE_AT, delta=0.040)

    def test_result_describes_the_cut(self):
        for policy, fps in (('original', 24), ('interpolated', 48)):
            res = self.outs[policy][0]
            self.assertEqual(res['scenes'], 3)
            self.assertAlmostEqual(res['seconds'], EXPECTED_FILM_SECONDS, delta=1 / fps)
            self.assertEqual(res['cut_plan']['export']['fps'],
                             finishing.output_fps(24, finishing.clean_finishing(
                                 {'fps_policy': policy})))
            self.assertRegex(res['fingerprint'], r'^[0-9a-f]{64}$')


# ------------------------------------------------------- legacy M4 goldens stay byte-equal

@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not installed')
class LegacyGoldenParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        projects.ROOT = _inproc      # see _ROOT_ORIG note at the imports
        cls.root = projects.ROOT
        legacy_m4.install(cls.root)
        cls.before = (cls.root / legacy_m4.SLUG / 'project.json').read_bytes()
        cls.p = projects.load(legacy_m4.SLUG)

    @staticmethod
    def golden(name):
        return (GOLD / name).read_text()

    def test_legacy_project_loads_and_reads_are_side_effect_free(self):
        self.assertNotIn('finishing', self.p)
        for scene in self.p['scenes']:
            projects.scene_status(self.p, scene)  # must not raise on a pre-M5 take
        self.assertEqual((self.root / legacy_m4.SLUG / 'project.json').read_bytes(),
                         self.before, 'reading a legacy project must not write it')
        self.assertNotIn('finishing', json.loads(self.before))
        self.assertEqual(capture_legacy_m4.canon(legacy_m4.project_dict(self.root),
                                                 str(self.root)),
                         self.golden('legacy_m4_project.json'))

    def test_cut_plans_stay_byte_equal(self):
        used = projects.current_used_takes(self.p)
        self.assertEqual(capture_legacy_m4.canon(
            projects.cut_plan(self.p, used, 'film'), str(self.root)),
            self.golden('legacy_m4_cut_plan_film.json'))
        self.assertEqual(capture_legacy_m4.canon(
            projects.cut_plan(self.p, used, 'animatic'), str(self.root)),
            self.golden('legacy_m4_cut_plan_animatic.json'))

    def test_enhance_graph_stays_byte_equal(self):
        self.assertEqual(capture_legacy_m4.canon(enhance.graph('take.mkv', 'golden-run'),
                                                 str(self.root)),
                         self.golden('legacy_m4_enhance_graph.json'))

    def test_job_finishing_only_adds_the_documented_keys(self):
        golden = json.loads(self.golden('legacy_m4_job_finishing.json'))
        head = web.job_finishing()
        added = set(head) - set(golden)
        self.assertEqual(added, {'settings', 'stages', 'output_fps'},
                         'job_finishing() may only grow by the documented M5 keys')
        subset = {k: v for k, v in head.items() if k in golden}
        self.assertEqual(capture_legacy_m4.canon(subset, str(self.root)),
                         self.golden('legacy_m4_job_finishing.json'))
        self.assertEqual(head['stages'], ['upscale', 'smooth', 'encode'])
        self.assertEqual(head['output_fps'], 48)

    def test_assemble_command_line_stays_byte_equal(self):
        # HEAD emitted yuv444p exports until the encode flags pinned '-pix_fmt yuv420p'
        # again (xfade negotiates yuv444p even for yuv420p inputs); with that fix the
        # command line is byte-identical to the M4 capture, allowlist-free.
        _res, cmd = fake_assemble(self.p)
        self.assertEqual(capture_legacy_m4.canon(cmd, str(self.root)),
                         self.golden('legacy_m4_assemble_cmd.json'))


# ------------------------------------------------- the switches are separate, end to end

@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not installed')
class SwitchSeparation(unittest.TestCase):
    slug = 'switch-sep'

    @classmethod
    def setUpClass(cls):
        projects.ROOT = _inproc      # see _ROOT_ORIG note at the imports
        install_acc(projects.ROOT, cls.slug)
        cls.st_off = finishing.clean_finishing(
            {'fps_policy': 'original', 'upscale': {'enabled': False}})
        cls.st_crf20 = finishing.clean_finishing(
            {'fps_policy': 'original', 'upscale': {'enabled': False},
             'encode': {'crf': 20}})
        cls.st_interp = finishing.clean_finishing(
            {'fps_policy': 'interpolated', 'upscale': {'enabled': False}})

    def test_graph_and_stages_shrink_when_switches_are_off(self):
        full = enhance.graph('take.mkv', 'r')
        off = enhance.graph('take.mkv', 'r', settings=self.st_off)
        self.assertEqual(sorted(full), [str(n) for n in range(1, 7)])
        self.assertEqual(sorted(off), ['1', '6'], 'encode-only graph: load in, encode out')
        self.assertEqual(full['6']['inputs']['images'], ['5', 0])
        self.assertEqual(off['6']['inputs']['images'], ['1', 0])
        self.assertEqual(full['6']['inputs']['frame_rate'], 48)
        self.assertEqual(off['6']['inputs']['frame_rate'], 24)
        self.assertEqual(finishing.enabled_stages(finishing.defaults()),
                         ['upscale', 'smooth', 'encode'])
        self.assertEqual(finishing.enabled_stages(self.st_off), ['encode'])
        self.assertEqual(web.scene_stage_keys('enhance', finishing.defaults()),
                         ['upscale', 'smooth', 'encode'])
        self.assertEqual(web.scene_stage_keys('enhance', self.st_off), ['encode'])
        self.assertEqual(web.scene_stage_keys('render-final', finishing.defaults()),
                         ['render', 'upscale', 'smooth', 'encode'])
        self.assertEqual(web.scene_stage_keys('render-final', self.st_off),
                         ['render', 'encode'])

    def test_fps_policy_alone_changes_only_the_export_component(self):
        set_finishing(self.slug, self.st_off)
        p = projects.load(self.slug)
        plan_x = projects.cut_plan(p, projects.current_used_takes(p), 'film')
        set_finishing(self.slug, self.st_interp)
        p = projects.load(self.slug)
        plan_z = projects.cut_plan(p, projects.current_used_takes(p), 'film')
        changed = [c for c in projects.CUT_COMPONENTS if plan_x[c] != plan_z[c]]
        self.assertEqual(changed, ['export'],
                         'fps_policy must not move takes/transitions/cards/music')
        self.assertEqual(plan_x['export']['fps'], 24)
        self.assertEqual(plan_z['export']['fps'], 48)
        self.assertNotEqual(projects._plan_fingerprint(plan_x),
                            projects._plan_fingerprint(plan_z))

    def test_crf_alone_changes_only_encode_args(self):
        set_finishing(self.slug, self.st_off)
        res_a, cmd_a = fake_assemble(projects.load(self.slug))
        set_finishing(self.slug, self.st_crf20)
        res_b, cmd_b = fake_assemble(projects.load(self.slug))
        # each assemble renders its cards into a fresh mkdtemp() and stamps the
        # output name with the current second; fold both away
        strip = lambda cmd: [re.sub(r'/tmp/tmp[a-z0-9_]{8}(?=/)', '<cards>',
                                    re.sub(r'film-\d{8}-\d{6}\.mp4', 'film-OUT.mp4', tok))
                             for tok in cmd]
        cmd_a, cmd_b = strip(cmd_a), strip(cmd_b)
        ia, ib = cmd_a.index('-crf'), cmd_b.index('-crf')
        self.assertEqual(ia, ib)
        self.assertEqual(cmd_a[ia + 1], '16')
        self.assertEqual(cmd_b[ib + 1], '20')
        self.assertEqual(cmd_a[:ia] + cmd_a[ia + 2:], cmd_b[:ib] + cmd_b[ib + 2:],
                         'crf must be the only difference in the command')
        changed = [c for c in projects.CUT_COMPONENTS
                   if res_a['cut_plan'][c] != res_b['cut_plan'][c]]
        self.assertEqual(changed, ['export'])
        self.assertEqual(projects.load(self.slug)['finishing']['encode']['crf'], 20)

    def test_output_status_names_export_as_the_only_stale_component(self):
        # stored entry from the st_off plan; the project now saves st_crf20
        set_finishing(self.slug, self.st_off)
        res_a, _cmd = fake_assemble(projects.load(self.slug))
        set_finishing(self.slug, self.st_crf20)
        st = projects.output_status(projects.load(self.slug), 'film', res_a)
        self.assertEqual(st['changes'], ['export'])
        self.assertTrue(st['outdated'])

    def test_legacy_scene_status_does_not_depend_on_the_settings(self):
        states = []
        for st in (self.st_off, self.st_crf20, self.st_interp):
            set_finishing(self.slug, st)
            p = projects.load(self.slug)
            states.append({
                'final': projects.scene_status(p, p['scenes'][0]),
                'no-final': projects.scene_status(p, p['scenes'][1]),
            })
        self.assertEqual(states[0], states[1], 'flags must not move with the switches')
        self.assertEqual(states[1], states[2])
        self.assertEqual(states[0]['no-final']['flags']['finishing_changed']['state'],
                         'not_applicable')


# ------------------------------------------- the HTTP gate: real Handler, stubbed backends

H3_TEXT = ('integrated_multimodal_description: A red kite climbs over a dusk field. '
           'At 00:02.0, [Shot 1] it catches the wind.\n'
           'overall_soundscape: Wind moves through grass.\n'
           'non_diegetic_music: None.')


class _DirectorStub(http.server.BaseHTTPRequestHandler):
    def _reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.partition('?')[0].rstrip('/').endswith('models'):
            self._reply({'data': [{'id': 'stub-model'}]})
        else:
            self._reply({'data': []})

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        self._reply({'id': 'stub', 'object': 'chat.completion', 'model': 'stub-model',
                     'choices': [{'index': 0, 'finish_reason': 'stop',
                                  'message': {'role': 'assistant', 'content': H3_TEXT}}],
                     'usage': {'completion_tokens': 42}})

    def log_message(self, fmt, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_http(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise AssertionError(f'{url} never answered')


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not installed')
class WebFinishingGate(unittest.TestCase):
    slug = 'finishing-acc'

    @classmethod
    def setUpClass(cls):
        cls.director = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _DirectorStub)
        threading.Thread(target=cls.director.serve_forever, daemon=True).start()
        cls.dport = cls.director.server_address[1]

        cls.comfyroot = MODROOT / 'comfy1'
        cls.stub, cls.sport = cls._start_stub(cls.comfyroot)

        cls.root1 = MODROOT / 'web1'
        legacy_m4.install(cls.root1)
        install_acc(cls.root1, cls.slug)
        cls.web1 = None
        try:
            cls.web1, cls.b1 = cls._start_web(cls.root1, cls.comfyroot, cls.sport)
        except Exception:
            cls.stub.terminate()
            cls.stub.wait(timeout=10)
            cls.director.shutdown()
            raise

    # -- harness ------------------------------------------------------------------

    @staticmethod
    def _start_stub(root, drop=(), port=None):
        port = port or free_port()
        env = dict(os.environ, COMFY_URL=f'http://127.0.0.1:{port}',
                   COMFY_ROOT=str(root))
        if drop:
            env['COMFY_STUB_DROP'] = ','.join(drop)
        proc = subprocess.Popen([sys.executable, str(HERE / 'stub_comfy.py'),
                                 '--port', str(port), '--root', str(root)],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        wait_http(f'http://127.0.0.1:{port}/object_info')
        return proc, port

    @classmethod
    def _start_web(cls, root, comfyroot, sport, path_prefix=None):
        port = free_port()
        env = dict(os.environ,
                   PROJECTS_ROOT=str(root),
                   JOBS_DB=str(MODROOT / f'jobs-{port}.db'),
                   STATS=str(MODROOT / f'stats-{port}.json'),
                   COMFY_ROOT=str(comfyroot),
                   COMFY_URL=f'http://127.0.0.1:{sport}',
                   DIRECTOR_URL=f'http://127.0.0.1:{cls.dport}/v1/chat/completions',
                   DIRECTOR_MODEL='stub-model',
                   POLL_SECONDS='0.15', RECONCILE_SECONDS='60',
                   READINESS_CACHE='0.05')
        if path_prefix:
            env['PATH'] = str(path_prefix) + os.pathsep + os.environ['PATH']
        proc = subprocess.Popen([sys.executable, str(REPO / 'web.py'),
                                 '--port', str(port)],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        wait_http(f'http://127.0.0.1:{port}/api/config')
        return proc, port

    @classmethod
    def tearDownClass(cls):
        for proc in (getattr(cls, 'web1', None), getattr(cls, 'web2', None),
                     getattr(cls, 'stub', None)):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        cls.director.shutdown()

    def api(self, port, method, path, body=None):
        url = f'http://127.0.0.1:{port}{path}'
        raw = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=raw, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode()
                return r.status, json.loads(text)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                return e.code, json.loads(text)
            except json.JSONDecodeError:
                return e.code, text

    def post(self, port, path, body):
        return self.api(port, 'POST', path, body)

    def readiness(self, port, project=None):
        q = '?refresh=1' + (f'&project={project}' if project else '')
        status, body = self.api(port, 'GET', f'/api/readiness/finishing{q}')
        self.assertEqual(status, 200)
        return body

    def restart_stub(self, drop=()):
        # class attr: unittest builds a fresh instance per test method, so an
        # instance attr set by an earlier test would be invisible here
        self.stub.terminate()
        self.stub.wait(timeout=10)
        self.__class__.stub, _ = self._start_stub(self.comfyroot, drop, self.sport)

    # -- tests (a..f fixes the execution order the backends need) -------------------

    def test_a_legacy_get_is_read_only(self):
        f = self.root1 / legacy_m4.SLUG / 'project.json'
        before = f.read_bytes()
        self.assertNotIn('finishing', json.loads(before))
        status, view = self.api(self.b1, 'GET', f'/api/p/{legacy_m4.SLUG}')
        self.assertEqual(status, 200)
        self.assertIn('scenes', view)
        self.assertEqual(f.read_bytes(), before,
                         'GET must not write a finishing key into a legacy project')

    def test_b_readiness_is_ready_against_the_full_stub(self):
        rep = self.readiness(self.b1, project=self.slug)
        self.assertEqual(rep['level'], 'ready', rep['summary'])
        status, rep2 = self.api(self.b1, 'GET', '/api/readiness')
        self.assertEqual(status, 200)
        for name in ('prompt', 'video', 'finishing'):
            self.assertEqual(rep2['checks'][name]['level'], 'ready',
                             f'{name}: {rep2["checks"][name]["summary"]}')

    def test_c_seedvr2_missing_blocks_finishing_actions_only(self):
        self.restart_stub(drop=('SeedVR2LoadDiTModel', 'SeedVR2LoadVAEModel',
                                'SeedVR2VideoUpscaler'))
        rep = self.readiness(self.b1, project=self.slug)
        self.assertEqual(rep['level'], 'unavailable')
        self.assertIn('SeedVR2LoadDiTModel', rep['reason'])
        self.assertIn('not installed in ComfyUI', rep['reason'])
        self.assertIn('(affects upscale)', rep['reason'])
        self.assertNotIn('RIFE', rep['reason'])
        self.assertNotIn('interpolation', rep['reason'])

        # prompt and a DRAFT render stay open
        status, _ = self.post(self.b1, f'/api/p/{self.slug}/prompt', {'scene': '11aa11'})
        self.assertEqual(status, 200)
        status, _ = self.post(self.b1, f'/api/p/{self.slug}/render',
                              {'scene': '11aa11', 'preset': 'draft'})
        self.assertEqual(status, 200)
        # the finishing actions are gated
        st, body = self.post(self.b1, f'/api/p/{self.slug}/enhance', {'scene': '22bb22'})
        self.assertEqual(st, 503)
        self.assertIn('SeedVR2', body['error'])
        st, body = self.post(self.b1, f'/api/p/{self.slug}/render',
                             {'scene': '22bb22', 'preset': 'final'})
        self.assertEqual(st, 503)
        self.assertIn('SeedVR2', body['error'])
        self.assertEqual(self.post(self.b1, f'/api/p/{self.slug}/assemble',
                                   {'kind': 'film'})[0], 503)

        # ...and switching upscaling OFF in the project excuses the SeedVR2 nodes
        st, _ = self.post(self.b1, f'/api/p/{self.slug}/save',
                          {'finishing': {'upscale': {'enabled': False}}})
        self.assertEqual(st, 200)
        rep = self.readiness(self.b1, project=self.slug)
        self.assertEqual(rep['level'], 'ready',
                         f'upscale off should excuse SeedVR2: {rep["summary"]}')
        st, _ = self.post(self.b1, f'/api/p/{self.slug}/enhance', {'scene': '33cc33'})
        self.assertEqual(st, 200)

    def test_d_rife_missing_blocks_interpolation_actions_only(self):
        self.restart_stub(drop=('RIFE VFI',))
        rep = self.readiness(self.b1, project=self.slug)
        self.assertEqual(rep['level'], 'unavailable')
        self.assertIn('RIFE VFI', rep['reason'])
        self.assertIn('(affects interpolation)', rep['reason'])
        self.assertNotIn('SeedVR2', rep['reason'])

        st, _ = self.post(self.b1, f'/api/p/{self.slug}/render',
                          {'scene': '33cc33', 'preset': 'final'})
        self.assertEqual(st, 503)
        st, _ = self.post(self.b1, f'/api/p/{self.slug}/enhance', {'scene': '33cc33'})
        self.assertEqual(st, 503)

        # interpolation off (original fps) -> the RIFE nodes are excused
        st, _ = self.post(self.b1, f'/api/p/{self.slug}/save',
                          {'finishing': {'fps_policy': 'original'}})
        self.assertEqual(st, 200)
        self.assertEqual(self.readiness(self.b1, project=self.slug)['level'], 'ready')
        st, _ = self.post(self.b1, f'/api/p/{self.slug}/enhance', {'scene': '22bb22'})
        self.assertEqual(st, 200)

    def test_e_vhs_missing_blocks_finishing_not_render(self):
        self.restart_stub(drop=('VHS_LoadVideoPath', 'VHS_VideoCombine'))
        rep = self.readiness(self.b1, project=self.slug)
        self.assertEqual(rep['level'], 'unavailable')
        self.assertIn('VHS_VideoCombine', rep['reason'])
        self.assertIn('(affects encode)', rep['reason'])
        self.assertNotIn('SeedVR2', rep['reason'])
        self.assertNotIn('interpolation', rep['reason'])

        self.assertEqual(self.post(self.b1, f'/api/p/{self.slug}/enhance',
                                   {'scene': '11aa11'})[0], 503)
        self.assertEqual(self.post(self.b1, f'/api/p/{self.slug}/assemble',
                                   {'kind': 'film'})[0], 503)
        # rendering is untouched by the missing VHS nodes
        status, rep2 = self.api(self.b1, 'GET', '/api/readiness/video')
        self.assertEqual(status, 200)
        self.assertEqual(rep2['level'], 'ready', rep2['summary'])

    def test_f_fake_ffmpeg_without_xfade_names_xfade(self):
        fakebin = MODROOT / 'fakebin'
        fakebin.mkdir(exist_ok=True)
        real = shutil.which('ffmpeg')
        (fakebin / 'ffmpeg').write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-hide_banner" ] && [ "$2" = "-filters" ]; then\n'
            f'    {real} -hide_banner -filters | grep -v " xfade "\n'
            '    exit 0\n'
            'fi\n'
            f'exec {real} "$@"\n')
        (fakebin / 'ffmpeg').chmod(0o755)
        root2 = MODROOT / 'web2'
        install_acc(root2, self.slug)
        self.restart_stub()  # full node set again: only ffmpeg should be broken
        self.web2, self.b2 = self._start_web(root2, self.comfyroot, self.sport,
                                             path_prefix=fakebin)
        rep = self.readiness(self.b2, project=self.slug)
        self.assertEqual(rep['level'], 'unavailable')
        self.assertIn('ffmpeg lacks export filters', rep['summary'])
        self.assertIn('this ffmpeg lacks xfade', rep['reason'])
        self.assertNotIn('loudnorm', rep['reason'])

        st, body = self.post(self.b2, f'/api/p/{self.slug}/enhance', {'scene': '22bb22'})
        self.assertEqual(st, 503)
        self.assertIn('xfade', body['error'])
        self.assertEqual(self.post(self.b2, f'/api/p/{self.slug}/assemble',
                                   {'kind': 'film'})[0], 503)
        st, _ = self.post(self.b2, f'/api/p/{self.slug}/prompt', {'scene': '22bb22'})
        self.assertEqual(st, 200)
        st, _ = self.post(self.b2, f'/api/p/{self.slug}/render',
                          {'scene': '22bb22', 'preset': 'draft'})
        self.assertEqual(st, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
