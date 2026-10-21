"""Centralised configuration (config.py): defaults match AGENTS.md, precedence is
flag > env > config.json > default, every legacy env var keeps its meaning, bad values and
malformed files fail fast naming the setting/file, unknown config.json keys warn and are
ignored, secrets are masked in /api/config, the ComfyUI websocket follows a moved COMFY,
and the module globals are exactly what config resolved.

    python3 -m unittest discover tests      (from the repo root)

Nothing here starts ComfyUI or touches projects/: loads pass explicit argv/env/config_file.
The /api/config test drives the real web.Handler on a spare port (as in test_status.py).
"""
import contextlib, io, json, os, sys, tempfile, threading, unittest, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import director, enhance, jobs_db, library, project, stats, web

HERE = Path(__file__).resolve().parent.parent
ABSENT = str(HERE / 'tests' / '_no_such_config.json')


def load(argv=(), env=None, config_file=ABSENT):
    return config.load(argv=list(argv), env=dict(env or {}), config_file=config_file)


class Defaults(unittest.TestCase):
    def test_defaults_match_the_documented_addresses(self):
        cfg = load()
        self.assertEqual(cfg.value('ui_host'), '127.0.0.1')
        self.assertEqual(cfg.value('ui_port'), 8192)
        self.assertEqual(cfg.value('comfy_url'), 'http://127.0.0.1:8190')
        self.assertEqual([u for u, _ in cfg.value('director_backends')],
                         ['http://127.0.0.1:8144/v1/chat/completions',
                          'http://127.0.0.1:8078/v1/chat/completions'])
        self.assertEqual([m for _, m in cfg.value('director_backends')], ['qwen3.8-27b', 'qwen3.6-35b-a3b'])
        self.assertEqual(cfg.value('projects_root'), HERE / 'projects')
        self.assertEqual(cfg.value('comfy_root'), HERE.parent / 'ComfyUI')
        self.assertEqual(cfg.value('stats_file'), HERE / 'stats.json')
        self.assertEqual(cfg.value('jobs_db_file'), HERE / 'jobs.db')
        self.assertEqual(cfg.value('ffmpeg'), 'ffmpeg')
        self.assertEqual(cfg.value('ffprobe'), 'ffprobe')
        self.assertEqual(cfg.value('job_timeout'), 4 * 3600)
        self.assertEqual(cfg.value('poll_seconds'), 1.5)
        self.assertEqual(cfg.value('reconcile_seconds'), 15)
        self.assertEqual((cfg.value('xfade_seconds'), cfg.value('dip_seconds')), (0.5, 0.4))
        self.assertEqual(cfg.value('preview_export_fps'), 24)
        self.assertEqual(cfg.value('preview_export_crf'), 20)
        self.assertEqual(cfg.value('comfy_client_id'), 'freevideo-director')
        self.assertEqual({k: (v['value'], v['source']) for k, v in cfg.api_view()['settings'].items()
                          if v['source'] != 'default'}, {})

    def test_summary_names_the_effective_addresses(self):
        line = load().summary()
        self.assertIn('UI http://127.0.0.1:8192', line)
        self.assertIn('ComfyUI http://127.0.0.1:8190', line)
        self.assertIn('websocket 127.0.0.1:8190', line)
        self.assertIn('127.0.0.1:8144', line)
        pinned = load(env={'DIRECTOR_URL': 'http://127.0.0.1:9000/v1/chat/completions',
                           'DIRECTOR_MODEL': 'm-9000'})
        self.assertIn('pinned to http://127.0.0.1:9000/v1/chat/completions (m-9000)', pinned.summary())


class Precedence(unittest.TestCase):
    def test_flag_beats_env_beats_file_beats_default(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'config.json'
            f.write_text(json.dumps({'ui_port': 7001, 'poll_seconds': 2.5, 'comfy_url': 'http://10.0.0.2:8190'}))
            cfg = load(argv=['--port', '7002'], env={'UI_PORT': '7003', 'POLL_SECONDS': '3.5'}, config_file=f)
            self.assertEqual((cfg.value('ui_port'), cfg.source('ui_port')), (7002, 'flag --port'))
            self.assertEqual((cfg.value('poll_seconds'), cfg.source('poll_seconds')), (3.5, 'env POLL_SECONDS'))
            self.assertEqual((cfg.value('comfy_url'), cfg.source('comfy_url')), ('http://10.0.0.2:8190', str(f)))
            plain = load(config_file=f)
            self.assertEqual((plain.value('ui_port'), plain.source('ui_port')), (7001, str(f)))

    def test_legacy_env_names_keep_their_meaning(self):
        env = {'COMFY_URL': 'http://127.0.0.1:9900', 'COMFY_ROOT': '/tmp/comfy-alt',
               'DIRECTOR_URL': 'http://127.0.0.1:1234/v1/chat/completions', 'DIRECTOR_MODEL': 'alt-model',
               'PROJECTS_ROOT': '/tmp/projects-alt', 'STATS': '/tmp/stats-alt.json',
               'JOBS_DB': '/tmp/jobs-alt.db', 'JOB_TIMEOUT': '60', 'POLL_SECONDS': '0.15',
               'RECONCILE_SECONDS': '1.2', 'DIRECTOR_NOTIFY_URL': 'https://ntfy.example.org/topic',
               'UI_HOST': '0.0.0.0', 'UI_PORT': '8300', 'UI_ALLOW_HOSTS': 'a.example, b.example'}
        cfg = load(env=env)
        self.assertEqual(cfg.value('comfy_url'), 'http://127.0.0.1:9900')
        self.assertEqual(cfg.value('comfy_root'), Path('/tmp/comfy-alt'))
        self.assertEqual((cfg.value('director_url'), cfg.value('director_model')),
                         ('http://127.0.0.1:1234/v1/chat/completions', 'alt-model'))
        self.assertEqual(cfg.value('projects_root'), Path('/tmp/projects-alt'))
        self.assertEqual(cfg.value('stats_file'), Path('/tmp/stats-alt.json'))
        self.assertEqual(cfg.value('jobs_db_file'), Path('/tmp/jobs-alt.db'))
        self.assertEqual((cfg.value('job_timeout'), cfg.value('poll_seconds'), cfg.value('reconcile_seconds')),
                         (60.0, 0.15, 1.2))
        self.assertEqual(cfg.value('notify_url'), 'https://ntfy.example.org/topic')
        self.assertEqual((cfg.value('ui_host'), cfg.value('ui_port')), ('0.0.0.0', 8300))
        self.assertEqual(cfg.value('ui_allow_hosts'), ('a.example', 'b.example'))

    def test_empty_env_string_is_unset_like_before(self):
        # the modules used `os.environ.get(X) or default`: an empty value never took effect
        cfg = load(env={'DIRECTOR_URL': '', 'POLL_SECONDS': '', 'COMFY_URL': ''})
        self.assertEqual(cfg.value('director_url'), '')
        self.assertEqual(cfg.value('poll_seconds'), 1.5)
        self.assertEqual(cfg.value('comfy_url'), 'http://127.0.0.1:8190')


class FailFast(unittest.TestCase):
    def test_malformed_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'config.json'
            f.write_text('{"ui_port": 8192,,}')
            with self.assertRaises(config.ConfigError) as cm:
                load(config_file=f)
            self.assertIn(str(f), str(cm.exception))

    def test_unknown_keys_warn_on_stderr_and_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'config.json'
            f.write_text(json.dumps({'comfy_port_typo': 8190, 'ui_port': 8400}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = load(config_file=f)
            self.assertIn("ignoring unknown setting 'comfy_port_typo'", err.getvalue())
            self.assertIn(str(f), err.getvalue())
            self.assertEqual(cfg.value('ui_port'), 8400)

    def test_invalid_values_name_the_setting_and_its_source(self):
        for env, needle in (({'UI_PORT': 'not-a-port'}, 'ui_port'),
                            ({'UI_PORT': '99999'}, 'ui_port'),
                            ({'COMFY_URL': 'ftp://nowhere'}, 'comfy_url'),
                            ({'POLL_SECONDS': '0'}, 'poll_seconds')):
            with self.subTest(**env), self.assertRaises(config.ConfigError) as cm:
                load(env=env)
            msg = str(cm.exception)
            self.assertIn(needle, msg)
            self.assertIn('env ' + next(iter(env)), msg)

    def test_dir_setting_pointing_at_a_file_fails(self):
        with tempfile.NamedTemporaryFile() as tf:
            with self.assertRaises(config.ConfigError) as cm:
                load(env={'PROJECTS_ROOT': tf.name})
        self.assertIn('projects_root', str(cm.exception))


class Secrets(unittest.TestCase):
    def test_notify_url_is_masked_in_api_view_but_configured_in_full(self):
        cfg = load(env={'DIRECTOR_NOTIFY_URL': 'https://user:topsecret@ntfy.example.org/secret-topic'})
        entry = cfg.api_view()['settings']['notify_url']
        self.assertTrue(entry['secret'])
        self.assertEqual(entry['value'], 'https://***@ntfy.example.org/***')
        self.assertNotIn('topsecret', entry['value'])
        self.assertNotIn('secret-topic', entry['value'])
        self.assertIn('ntfy.example.org', entry['value'])
        self.assertEqual(cfg.value('notify_url'), 'https://user:topsecret@ntfy.example.org/secret-topic')
        self.assertNotIn('topsecret', cfg.summary())
        bare = load(env={'DIRECTOR_NOTIFY_URL': 'https://ntfy.example.org/secret-topic'})
        self.assertEqual(bare.api_view()['settings']['notify_url']['value'], 'https://ntfy.example.org/***')


class ApiEndpoint(unittest.TestCase):
    def test_get_api_config_answers_effective_secret_free_settings(self):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)   # the real handler
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{srv.server_port}/api/config', timeout=10) as r:
                body = json.loads(r.read())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(set(body), {'config_file', 'derived', 'settings'})
        self.assertEqual(body['derived']['ws_listen']['value'], '127.0.0.1:8190')
        self.assertTrue(body['config_file']['path'].endswith('config.json'))
        self.assertEqual(set(body['settings']), set(config.SETTINGS))
        for name, entry in body['settings'].items():
            ok = (entry['source'] == 'default' or entry['source'].startswith('flag ')
                  or entry['source'].startswith('env ') or entry['source'] == config.CFG.config_file.as_posix())
            self.assertTrue(ok, f'{name}: {entry["source"]}')
        self.assertTrue(body['settings']['notify_url']['secret'])


class WebsocketFollowsComfy(unittest.TestCase):
    def test_ws_target_derives_from_the_live_comfy_global(self):
        import web
        web.COMFY, saved = 'http://127.0.0.1:9999', web.COMFY
        try:
            self.assertEqual(web.ws_target(), ('127.0.0.1', 9999))
            web.COMFY = 'http://example.org'
            self.assertEqual(web.ws_target(), ('example.org', 80))
        finally:
            web.COMFY = saved
        self.assertEqual(web.ws_target(), ('127.0.0.1', 8190))

    def test_ws_listen_connects_to_the_target_and_the_host_header_matches(self):
        import web

        class Stop(Exception):
            pass

        calls, sent = [], []

        class FakeSocket:
            def sendall(self, data):
                sent.append(data.decode())
                raise Stop()

        class FakeSocketModule:
            @staticmethod
            def create_connection(target, timeout=None):
                calls.append(target)
                return FakeSocket()

        real_socket, real_sleep = web.socket, web.time.sleep
        web.socket, web.time.sleep = FakeSocketModule, (lambda s: (_ for _ in ()).throw(Stop()))
        try:
            self.assertRaises(Stop, web.ws_listen)
        finally:
            web.socket, web.time.sleep = real_socket, real_sleep
        self.assertEqual(calls, [('127.0.0.1', 8190)])            # default COMFY
        self.assertIn('Host: 127.0.0.1:8190', sent[0])
        self.assertIn(f'clientId={web.CLIENT_ID}', sent[0])


class ModulesReadConfig(unittest.TestCase):
    def test_module_globals_are_the_configured_values(self):
        import director, enhance, jobs_db, library, project, stats, web
        for name, mod, attr in (('director_backends', director, 'BACKENDS'),
                                ('comfy_client_id', web, 'CLIENT_ID'),
                                ('presets', web, 'PRESETS'),
                                ('projects_root', project, 'ROOT'),
                                ('library_root', library, 'ROOT'),
                                ('stats_file', stats, 'FILE'),
                                ('jobs_db_file', jobs_db, 'FILE'),
                                ('enhance_fps', enhance, 'FPS'),
                                ('ffmpeg', enhance, 'FFMPEG')):
            self.assertEqual(getattr(mod, attr), config.SETTINGS[name].default, attr)
        self.assertEqual(enhance.DEFAULTS, {'resolution': 1080, 'multiplier': 2, 'crf': 16,
                                            'upscaler': 'seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors',
                                            'batch_size': 21, 'temporal_overlap': 3, 'rife': 'rife49.pth'})
        self.assertEqual((enhance.COMFY, enhance.COMFY_ROOT),
                         (config.SETTINGS['comfy_url'].default, config.SETTINGS['comfy_root'].default))
        self.assertEqual(web.COMFY_OUTPUT, enhance.COMFY_ROOT / 'output')
        self.assertEqual(web.JOB_TIMEOUT, config.SETTINGS['job_timeout'].default)
        self.assertEqual(web.POLL_SECONDS, config.SETTINGS['poll_seconds'].default)
        self.assertEqual(web.RECONCILE_SECONDS, config.SETTINGS['reconcile_seconds'].default)
        self.assertEqual((project.XFADE, project.DIP),
                         (config.SETTINGS['xfade_seconds'].default, config.SETTINGS['dip_seconds'].default))
        self.assertEqual((project.PREVIEW_FPS, project.PREVIEW_CRF), (24, 20))

    def test_export_spec_keeps_numeric_fps_and_string_crf(self):
        import project
        plan = project.cut_plan({'slug': 'x', 'aspect': '16:9', 'size': (1344, 768), 'scenes': [],
                                 'title_card': None, 'end_card': None}, [], 'film')
        self.assertEqual(plan['export']['fps'], 48)
        self.assertEqual(plan['export']['crf'], '16')
        anim = project.cut_plan({'slug': 'x', 'aspect': '16:9', 'size': (1344, 768), 'scenes': [],
                                 'title_card': None, 'end_card': None}, [], 'animatic')
        self.assertEqual((anim['export']['fps'], anim['export']['crf']), (24, '20'))

    def test_config_json_is_gitignored(self):
        self.assertIn('config.json', (HERE / '.gitignore').read_text().split('\n'))


class InProcessReload(unittest.TestCase):
    """The env hooks earlier steps installed still redirect at the process level after a
    reload -- what subprocess tests rely on; module globals keep their snapshot."""
    KEYS = ('PROJECTS_ROOT', 'STATS', 'JOBS_DB', 'COMFY_URL', 'CONFIG_FILE')

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.reload()

    def test_reload_picks_up_process_env(self):
        import os
        os.environ['PROJECTS_ROOT'] = '/tmp/m5s1-projects'
        os.environ['STATS'] = '/tmp/m5s1-stats.json'
        os.environ['JOBS_DB'] = '/tmp/m5s1-jobs.db'
        os.environ['COMFY_URL'] = 'http://127.0.0.1:1'
        os.environ['CONFIG_FILE'] = ABSENT
        config.reload()
        self.assertEqual(config.value('projects_root'), Path('/tmp/m5s1-projects'))
        self.assertEqual(config.value('stats_file'), Path('/tmp/m5s1-stats.json'))
        self.assertEqual(config.value('jobs_db_file'), Path('/tmp/m5s1-jobs.db'))
        self.assertEqual(config.value('comfy_url'), 'http://127.0.0.1:1')
        self.assertEqual(config.ws_target(), ('127.0.0.1', 1))


if __name__ == '__main__':
    unittest.main()
