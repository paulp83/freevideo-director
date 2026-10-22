#!/usr/bin/env python3
"""The three readiness checks: prompt / video generation / finishing.

Probes run against in-process stubs: a fake ComfyUI answering /system_stats and
/object_info, a fake director answering /models. No real service is touched, and
no check ever blocks longer than its own timeout."""
import http.server
import json
import os
import shutil
import socket
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import config
import enhance
import readiness
import leak_guard

HERE = Path(__file__).resolve().parent
ABSENT = HERE / 'tests-fixtures' / 'no-such-file.json'


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Serves whatever its server object holds; one class, many stubs."""

    def do_GET(self):
        stub = self.server.stub
        if stub.delay:
            time.sleep(stub.delay)
        if self.path.startswith('/object_info'):
            stub.hits['object_info'] += 1
            body = json.dumps(stub.info).encode()
        elif self.path.startswith('/system_stats'):
            stub.hits['system_stats'] += 1
            body = b'{"system": {}}'
        else:
            body = b'{"models": [{"id": "stub-director"}]}'   # /v1/models shape
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class StubServer(threading.Thread):
    """A ComfyUI-shaped stub; the class list and the model combos are configurable."""

    def __init__(self, info=None, delay=0.0, slow_path=None):
        super().__init__(daemon=True)
        self.info = info if info is not None else {}
        self.hits = {'system_stats': 0, 'object_info': 0}
        self.delay = delay
        self.slow_path = slow_path
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _StubHandler)
        self.httpd.stub = self
        self.port = self.httpd.server_address[1]
        self.base = f'http://127.0.0.1:{self.port}'

    def start(self):
        super().start()


    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def finishing_object_info(drop=(), without_files=True):
    """A /object_info body for every node enhance.graph() names, minus `drop`
    (substring, case-insensitive); model combos are included only when
    without_files is False."""
    g = enhance.graph('x.mp4', 'probe')
    info = {}
    for node in g.values():
        cls = str(node.get('class_type'))
        if any(d.upper() in cls.upper() for d in drop):
            continue
        combos = {}
        for k, v in (node.get('inputs') or {}).items():
            if isinstance(v, str) and v.endswith(('.safetensors', '.pth', '.pt')):
                # without_files: the node offers no option naming the model it loads
                combos[k] = [['None']] if without_files else [['None', v]]
        info[cls] = {'input': {'required': combos}}
    return info

def render_object_info():
    """The render half of /object_info; classes derived from web.build_graph."""
    import web
    info = {str(n.get('class_type')): {} for n in web._probe_render_graph().values()}
    info['FreeVideoLoRA'] = {'input': {'required': {'lora_name': [[
        'None', 'meadow-style.safetensors', 'gate-grain.safetensors']]}}}
    return info


def combined_object_info(*parts):
    info = {}
    for part in parts:
        info.update(part)
    return info


class ReadinessCase(unittest.TestCase):
    KEYS = ('COMFY_URL', 'DIRECTOR_URL', 'DIRECTOR_MODEL', 'READINESS_CACHE',
            'READINESS_TIMEOUT', 'CONFIG_FILE', 'COMFY_ROOT', 'FFMPEG', 'FFPROBE')

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix='m5s2-readiness-'))
        (self.tmp / 'input').mkdir()
        (self.tmp / 'output').mkdir()
        self._comfy_root = enhance.COMFY_ROOT
        enhance.COMFY_ROOT = self.tmp
        os.environ['CONFIG_FILE'] = str(ABSENT)
        os.environ['COMFY_ROOT'] = str(self.tmp)
        os.environ['READINESS_CACHE'] = '0.001'       # tests want live probes
        os.environ['READINESS_TIMEOUT'] = '2'
        config.reload()
        readiness.forget()
        # web.py registers the render-class probe with readiness when it imports; unit
        # tests stand in for that with the real module
        import web
        readiness.set_render_graph(web._probe_render_graph)

    def tearDown(self):
        enhance.COMFY_ROOT = self._comfy_root
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.reload()
        readiness.forget()

    def comfy(self, *a, **kw):
        srv = StubServer(*a, **kw)
        srv.start()
        self.addCleanup(srv.stop)
        os.environ['COMFY_URL'] = srv.base
        config.reload()
        readiness.forget()        # never trust a probe made against a dead stub
        return srv


class TestCheckShape(ReadinessCase):
    def test_report_shape_and_levels(self):
        comfy = self.comfy(combined_object_info(
            finishing_object_info(without_files=False), render_object_info()))
        os.environ['DIRECTOR_URL'] = comfy.base + '/v1/chat/completions'
        os.environ['DIRECTOR_MODEL'] = 'stub-director'
        config.reload()
        readiness.forget()
        rep = readiness.report()
        self.assertEqual(set(rep['checks']), set(readiness.CHECKS))
        for name, c in rep['checks'].items():
            self.assertEqual(c['name'], name)
            self.assertEqual(c['level'], 'ready', c['summary'])
            self.assertTrue(c['ok'])
            for item in c['items']:
                self.assertEqual(set(item), {'name', 'ok', 'short', 'detail',
                                             'how_to_fix', 'hard', 'affects'})

    def test_prompts_check_names_the_skill(self):
        comfy = self.comfy()
        os.environ['DIRECTOR_URL'] = comfy.base + '/v1/chat/completions'
        os.environ['DIRECTOR_MODEL'] = 'stub-director'
        config.reload()
        c = readiness.check('prompt')
        self.assertEqual(c['level'], 'ready', c['summary'])
        names = [i['name'] for i in c['items']]
        self.assertIn('model name', names)
        self.assertIn('H3 skill files', names)

    def test_all_three_are_independent(self):
        # ComfyUI down but director up: prompts ready, the other two unavailable.
        director = StubServer()
        director.start()
        self.addCleanup(director.stop)
        os.environ['DIRECTOR_URL'] = director.base + '/v1/chat/completions'
        os.environ['DIRECTOR_MODEL'] = 'stub-director'
        os.environ['COMFY_URL'] = 'http://127.0.0.1:1'
        config.reload()
        rep = readiness.report()
        self.assertEqual(rep['checks']['prompt']['level'], 'ready')
        self.assertEqual(rep['checks']['video']['level'], 'unavailable')
        self.assertEqual(rep['checks']['finishing']['level'], 'unavailable')
        self.assertTrue(rep['checks']['video']['reason'].startswith('Video generation unavailable'))


class TestGating(ReadinessCase):
    def test_missing_upscaler_nodes_only_break_finishing(self):
        comfy = self.comfy(combined_object_info(finishing_object_info(drop=('SEEDVR2',)), render_object_info()))
        try:
            readiness.require('video')
        except readiness.NotReady as e:
            self.fail(f'video must stay usable: {e}')
        with self.assertRaises(readiness.NotReady) as ctx:
            readiness.require('finishing')
        msg = str(ctx.exception)
        self.assertIn('SeedVR2', msg)
        self.assertIn('Finishing unavailable', msg)
        rep = readiness.report()
        self.assertEqual(rep['checks']['video']['level'], 'ready')
        self.assertEqual(rep['checks']['finishing']['level'], 'unavailable')

    def test_missing_model_files_only_break_finishing(self):
        # the stub lists the finishing node classes but not the model files they load
        comfy = self.comfy(combined_object_info(finishing_object_info(), render_object_info()))
        with self.assertRaises(readiness.NotReady) as ctx:
            readiness.require('finishing')
        msg = str(ctx.exception)
        self.assertIn('seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors', msg)
        self.assertIn('rife49.pth', msg)
        self.assertTrue(readiness.check('video')['ok'])

    def test_all_required_models_listed_when_present(self):
        g = enhance.graph('x.mp4', 'probe')
        files = set()
        for node in g.values():
            stack = [node.get('inputs')]
            while stack:
                v = stack.pop()
                if isinstance(v, str) and v.endswith(('.safetensors', '.pth', '.pt')):
                    files.add(v)
                elif isinstance(v, list):
                    stack.extend(v)
                elif isinstance(v, dict):
                    stack.extend(v.values())
        info = finishing_object_info(without_files=False)
        for cls, node in g.items():
            cls = str(node.get('class_type'))
            if cls in info:
                for spec in info[cls]['input']['required'].values():
                    spec[0].extend(sorted(files))
        self.comfy(combined_object_info(info, render_object_info()))
        c = readiness.check('finishing')
        self.assertEqual(c['level'], 'ready', c['summary'])
        self.assertTrue(readiness.require('finishing') is None)
        self.assertGreaterEqual(len(files), 1)

    def test_comfy_down_one_reason_not_a_dozen(self):
        os.environ['COMFY_URL'] = 'http://127.0.0.1:1'
        config.reload()
        c = readiness.check('finishing')
        self.assertEqual(c['level'], 'unavailable')
        hard = [i for i in c['items'] if not i['ok'] and i['hard']]
        self.assertEqual([i['name'] for i in hard], ['ComfyUI server'])
        self.assertIn('not reachable', c['reason'])
        self.assertNotIn('not installed', c['reason'])

class TestStageScoring(ReadinessCase):
    """Per-project scoring: a dependency of a stage the project's finishing settings switched
    off becomes a named non-issue instead of a blocker; the probe still sees the whole graph,
    so what was excused stays visible."""

    def setUp(self):
        super().setUp()
        import finishing
        self.no_upscale = finishing.clean_finishing({'upscale': {'enabled': False}})
        self.no_interp = finishing.clean_finishing({'fps_policy': 'original'})
        self.off_all = finishing.clean_finishing({'upscale': {'enabled': False},
                                                  'fps_policy': 'original'})

    def test_upscaling_off_excuses_the_seedvr2_nodes_and_its_model_file(self):
        # stub: SeedVR2 node classes missing AND (as always for this stub) no model files listed
        self.comfy(combined_object_info(finishing_object_info(drop=('SEEDVR2',)), render_object_info()))
        with self.assertRaises(readiness.NotReady) as ctx:
            readiness.require('finishing')             # projects that upscale still need it
        self.assertIn('SeedVR2', str(ctx.exception))
        off = readiness.check('finishing', settings=self.no_upscale)
        self.assertNotIn('SeedVR2', off['reason'] or '')
        self.assertIn('rife49.pth', off['reason'] or '')   # the RIFE stage still counts
        notes = [i for i in off['items'] if 'SeedVR2' in i['name'] or 'seedvr2' in i['name']]
        self.assertTrue(notes, [i['name'] for i in off['items']])
        self.assertTrue(all(i['ok'] and 'not needed' in i['short'] for i in notes),
                        [i['short'] for i in notes])
        self.assertTrue(any('Upscaling' in (i['detail'] or '') for i in notes))   # names the setting

    def test_interpolation_off_excuses_rife_and_its_checkpoint(self):
        self.comfy(combined_object_info(finishing_object_info(drop=('RIFE',)), render_object_info()))
        with self.assertRaises(readiness.NotReady) as ctx:
            readiness.require('finishing')
        self.assertIn('RIFE', str(ctx.exception))
        off = readiness.check('finishing', settings=self.no_interp)
        self.assertNotIn('rife49', off['reason'] or '')
        self.assertIn('seedvr2_ema', off['reason'] or '')
        notes = [i for i in off['items'] if 'rife' in i['name'].lower() or 'RIFE' in i['name']]
        self.assertTrue(notes and all(i['ok'] and 'not needed' in i['short'] for i in notes))

    def test_encode_only_project_needs_neither_upscaler_nor_interpolator(self):
        self.comfy(combined_object_info(finishing_object_info(drop=('SEEDVR2', 'RIFE')),
                                        render_object_info()))
        c = readiness.check('finishing', settings=self.off_all)
        self.assertEqual(c['level'], 'ready', c['summary'])
        readiness.require('finishing', settings=self.off_all)     # must not raise
        # the global view is untouched: without settings every stage still counts
        self.assertEqual(readiness.check('finishing')['level'], 'unavailable')

    def test_cache_scores_each_settings_variant_apart(self):
        self.comfy(combined_object_info(finishing_object_info(drop=('SEEDVR2', 'RIFE')),
                                        render_object_info()))
        os.environ['READINESS_CACHE'] = '60'
        config.reload()
        self.assertEqual(readiness.check('finishing')['level'], 'unavailable')
        self.assertEqual(readiness.check('finishing', settings=self.off_all)['level'], 'ready')
        self.assertEqual(readiness.check('finishing')['level'], 'unavailable')


class TestCacheAndTimeout(ReadinessCase):
    def test_answers_are_cached_until_refreshed(self):
        comfy = self.comfy(combined_object_info(finishing_object_info(), render_object_info()))
        os.environ['READINESS_CACHE'] = '60'
        config.reload()
        readiness.check('finishing')
        first = comfy.hits['object_info']
        readiness.check('finishing')          # served from cache
        self.assertEqual(comfy.hits['object_info'], first)
        readiness.check('finishing', refresh=True)
        self.assertEqual(comfy.hits['object_info'], first + 1)

    def test_a_stalled_server_does_not_stall_the_check(self):
        comfy = self.comfy(combined_object_info(finishing_object_info(), render_object_info()), delay=5.0)
        t0 = time.monotonic()
        c = readiness.check('video')
        spent = time.monotonic() - t0
        self.assertLess(spent, 4.0)
        self.assertEqual(c['level'], 'unavailable')
        self.assertIn('not reachable', c['summary'])


class TestMediaBin(ReadinessCase):
    def test_missing_export_filter_breaks_finishing(self):
        fake = self.tmp / 'fake-ffmpeg'
        fake.write_text('#!/bin/sh\n'
                        'case "$*" in\n'
                        '  *-filters*) echo "VSC afade"; echo "TSC concat"; echo "VSC fade";;\n'
                        '  *) exit 0;;\n'
                        'esac\n')
        fake.chmod(0o755)
        os.environ['FFMPEG'] = str(fake)
        os.environ['FFPROBE'] = str(fake)
        self.comfy(combined_object_info(finishing_object_info(), render_object_info()))
        config.reload()
        c = readiness.check('finishing')
        self.assertEqual(c['level'], 'unavailable')
        self.assertIn('xfade', c['reason'])
        self.assertTrue(readiness.check('video')['ok'])   # renders do not need the filters

    def test_export_filter_list_is_derived_from_project(self):
        self.comfy(combined_object_info(finishing_object_info(), render_object_info()))
        c = readiness.check('finishing')
        item = [i for i in c['items'] if i['name'] == 'ffmpeg filters for export'][0]
        self.assertTrue(item['ok'], item['detail'])
        self.assertIn(str(len(project_filters())), item['detail'])


def project_filters():
    import project
    return project.EXPORT_FILTERS


class TestRouteGates(ReadinessCase):
    """The server half of the contract: gated POSTs answer 503 with exactly the
    sentence the chips show; validation still outranks the gate and saving,
    starring and reading are never gated."""

    def setUp(self):
        super().setUp()
        import project as projects
        self.projects = projects
        self._saved_root = projects.ROOT
        projects.ROOT = self.tmp / 'projects'
        p = projects.create('Gate film', 'a logline', 20, '16:9', [])
        projects.mutate(p['slug'], lambda pr: pr.update(
            scenes=[projects.new_scene('One', 4, action='walk')]))
        self.slug = p['slug']
        self.sid = projects.load(self.slug)['scenes'][0]['id']
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.web_port = sock.getsockname()[1]
        sock.close()
        self.web_log = open(self.tmp / 'web.log', 'wb')
        self.web = leak_guard.spawn(
            [sys.executable, str(HERE.parent / 'web.py'), '--port', str(self.web_port)],
            cwd=str(HERE.parent),
            env=dict(os.environ, PROJECTS_ROOT=str(projects.ROOT),
                     JOBS_DB=str(self.tmp / 'jobs.db'), STATS=str(self.tmp / 'stats.json'),
                     COMFY_ROOT=str(self.tmp), COMFY_URL='http://127.0.0.1:1',
                     DIRECTOR_URL='http://127.0.0.1:1', READINESS_CACHE='0.001',
                     READINESS_TIMEOUT='1', CONFIG_FILE=str(ABSENT)),
            stdout=self.web_log, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self.http('GET', '/api/projects')
                break
            except OSError:
                time.sleep(0.2)
        else:
            # setUp raising skips tearDown: stop the server here or it leaks
            leak_guard.stop(self.web)
            self.fail('web.py did not start: ' + self.web_log.read().decode(errors='replace')[-400:])

    def tearDown(self):
        leak_guard.stop(self.web)
        self.web_log.close()
        self.projects.ROOT = self._saved_root
        super().tearDown()

    def http(self, method, path, body=None):
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.web_port}{path}',
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.getcode(), json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_gated_routes_answer_503_with_the_check_sentence(self):
        _, rep = self.http('GET', '/api/readiness')
        for name, c in rep['checks'].items():
            self.assertEqual(c['level'], 'unavailable', c['summary'])   # both services are down
        for action, check in (('prompt', 'prompt'), ('prompts', 'prompt'),
                              ('storyboard', 'prompt'), ('render', 'video'),
                              ('assemble', 'finishing')):
            code, body = self.http('POST', f'/api/p/{self.slug}/{action}',
                                   {'scene': self.sid, 'preset': 'draft'})
            self.assertEqual(code, 503, f'{action}: {body}')
            self.assertEqual(body['error'], rep['checks'][check]['reason'], action)
        code, body = self.http('POST', '/api/projects', {'name': 'New film', 'logline': 'x'})
        self.assertEqual(code, 503, body)
        self.assertEqual(body['error'], rep['checks']['prompt']['reason'])

    def test_validation_and_ungated_routes_are_never_503(self):
        code, body = self.http('POST', f'/api/p/{self.slug}/render',
                               {'scene': self.sid, 'preset': 'nonsense'})
        self.assertEqual(code, 400, body)                     # the preset is checked first
        self.assertIn('unknown preset', body['error'])
        code, body = self.http('POST', f'/api/p/{self.slug}/save', {'title': 'kept editing'})
        self.assertEqual(code, 200, body)                     # saving is never gated
        self.assertEqual(self.projects.load(self.slug)['title'], 'kept editing')
        code, body = self.http('POST', f'/api/p/{self.slug}/star', {'scene': self.sid, 'auto': True})
        self.assertEqual(code, 200, body)                     # choosing a take is never gated



if __name__ == '__main__':
    unittest.main()
