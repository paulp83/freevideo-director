#!/usr/bin/env python3
"""Centralised settings: service addresses, paths and defaults. Stdlib only, imports nothing
from this project.

Every module asks config for its addresses, paths and tunables instead of reading os.environ
or hard-coding ports. Precedence per setting: CLI flag > environment variable > config.json
(flat JSON next to this file, gitignored; location overridable with CONFIG_FILE) > built-in
default. Unknown settings in config.json warn on stderr and are ignored; a malformed file or an
invalid value raises ConfigError at startup, naming the setting and the source of the bad value.

    python3 -c 'import config; print(config.summary())'     # the effective configuration
    GET /api/config  (web.py)                                # the same, as JSON, secrets masked

Settings table (names, defaults, env vars, flags, users): docs/configuration.md.
"""
import argparse, copy, json, os, sys, urllib.parse
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE_NAME = 'config.json'
CONFIG_FILE_ENV = 'CONFIG_FILE'


class ConfigError(RuntimeError):
    """A bad setting value or a malformed config.json; the message names the setting/file."""


Setting = namedtuple('Setting', 'default env flag kind secret')

# kind: str | int | port | float | url | url-or-empty | dir | file | csv | backends | presets
SETTINGS = {
    # UI server
    'ui_host':            Setting('127.0.0.1', 'UI_HOST', '--host', 'str', False),
    'ui_port':            Setting(8192, 'UI_PORT', '--port', 'port', False),
    'ui_allow_hosts':     Setting((), 'UI_ALLOW_HOSTS', '--allow-host', 'csv', False),
    # ComfyUI (FreeVideo workspace)
    'comfy_url':          Setting('http://127.0.0.1:8190', 'COMFY_URL', '--comfy-url', 'url', False),
    'comfy_root':         Setting(HERE.parent / 'ComfyUI', 'COMFY_ROOT', '--comfy-root', 'dir', False),
    'comfy_client_id':    Setting('freevideo-director', 'COMFY_CLIENT_ID', None, 'str', False),
    # director model: explicit pin, else the search list below (AGENTS.md ports 8144 then 8078)
    'director_backends':  Setting([('http://127.0.0.1:8144/v1/chat/completions', 'qwen3.8-27b'),   # sgl single, best scripts
                                   ('http://127.0.0.1:8078/v1/chat/completions', 'qwen3.6-35b-a3b')],  # vllm single nvfp4, fastest
                                  None, None, 'backends', False),
    'director_url':       Setting('', 'DIRECTOR_URL', '--director-url', 'url-or-empty', False),
    'director_model':     Setting('', 'DIRECTOR_MODEL', '--director-model', 'str', False),
    # user-data paths
    'projects_root':      Setting(HERE / 'projects', 'PROJECTS_ROOT', '--projects-root', 'dir', False),
    'library_root':       Setting(HERE / 'library', 'LIBRARY_ROOT', '--library-root', 'dir', False),
    'out_dir':            Setting(HERE / 'out', 'OUT_DIR', '--out-dir', 'dir', False),
    'stats_file':         Setting(HERE / 'stats.json', 'STATS', '--stats', 'file', False),
    'jobs_db_file':       Setting(HERE / 'jobs.db', 'JOBS_DB', '--jobs-db', 'file', False),
    # notifications
    'notify_url':         Setting('', 'DIRECTOR_NOTIFY_URL', '--notify-url', 'url-or-empty', True),
    # external binaries (found on PATH)
    'ffmpeg':             Setting('ffmpeg', 'FFMPEG', None, 'str', False),
    'ffprobe':            Setting('ffprobe', 'FFPROBE', None, 'str', False),
    # job runner tunables
    'job_timeout':        Setting(4 * 3600, 'JOB_TIMEOUT', '--job-timeout', 'float', False),
    'poll_seconds':       Setting(1.5, 'POLL_SECONDS', '--poll-seconds', 'float', False),
    'reconcile_seconds':  Setting(15, 'RECONCILE_SECONDS', '--reconcile-seconds', 'float', False),
    # readiness probes (readiness.py)
    'readiness_cache':   Setting(10, 'READINESS_CACHE',   None, 'float', False),
    'readiness_timeout': Setting(2,  'READINESS_TIMEOUT', None, 'float', False),
    # finishing defaults (enhance.py)
    'enhance_fps':              Setting(24, 'ENHANCE_FPS', None, 'int', False),
    'enhance_resolution':       Setting(1080, 'ENHANCE_RESOLUTION', '--resolution', 'int', False),
    'enhance_multiplier':       Setting(2, 'ENHANCE_FPS_MULTIPLIER', '--fps-multiplier', 'int', False),
    'enhance_crf':              Setting(16, 'ENHANCE_CRF', '--crf', 'int', False),
    'enhance_batch_size':       Setting(21, 'ENHANCE_BATCH_SIZE', '--batch-size', 'int', False),
    'enhance_upscaler':         Setting('seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors',
                                        'ENHANCE_UPSCALER', None, 'str', False),
    'enhance_temporal_overlap': Setting(3, 'ENHANCE_TEMPORAL_OVERLAP', None, 'int', False),
    'enhance_rife':             Setting('rife49.pth', 'ENHANCE_RIFE', None, 'str', False),
    # film assembly defaults (project.py). The film export fps and crf are NOT settings any
    # more: they come from each project's finishing settings (see finishing.py); only the
    # animatic/preview cut keeps a draft policy of its own here.
    'preview_export_fps': Setting(24, 'PREVIEW_EXPORT_FPS', None, 'int', False),
    'preview_export_crf': Setting(20, 'PREVIEW_EXPORT_CRF', None, 'int', False),
    'xfade_seconds':      Setting(0.5, 'XFADE_SECONDS', None, 'float', False),
    'dip_seconds':        Setting(0.4, 'DIP_SECONDS', None, 'float', False),
    # render presets (web.py)
    'presets':            Setting({'draft': {'label': 'Draft', 'two_pass': True, 'finish': False},
                                   'final': {'label': 'Final', 'two_pass': False, 'finish': True}},
                                  None, None, 'presets', False),
}

SERVICE_FLAGS = ('comfy_url', 'comfy_root', 'projects_root', 'library_root', 'out_dir',
                 'stats_file', 'jobs_db_file', 'director_url', 'director_model', 'notify_url')


def _url(raw):
    sp = urllib.parse.urlsplit(str(raw).strip())
    if sp.scheme not in ('http', 'https') or not sp.netloc or not sp.hostname:
        raise ValueError(f'not an http(s) URL with a host: {raw!r}')
    sp.port                                    # ValueError when the port is not numeric
    return str(raw).strip()


def _parse(name, raw, source):
    """Convert and validate one raw value; ConfigError names the setting and its source."""
    kind = SETTINGS[name].kind

    def bad(why):
        raise ConfigError(f'config: invalid value for {name!r} from {source}: {why}') from None

    try:
        if kind == 'url-or-empty' and str(raw).strip() == '':
            return ''
        if kind == 'str':
            value = str(raw)
        elif kind == 'int':
            value = int(raw)
        elif kind == 'port':
            value = int(raw)
            if not 1 <= value <= 65535:
                bad(f'port must be 1-65535, got {value}')
        elif kind == 'float':
            value = float(raw)
            if value <= 0:
                bad(f'must be a positive number, got {value}')
        elif kind in ('url', 'url-or-empty'):
            value = _url(raw)
        elif kind in ('dir', 'file'):
            p = Path(str(raw)).expanduser()
            if kind == 'dir' and p.exists() and not p.is_dir():
                bad(f'not a directory: {p}')
            if kind == 'file' and p.exists() and not p.is_file():
                bad(f'not a file: {p}')
            value = p
        elif kind == 'csv':
            value = tuple(x.strip() for x in str(raw).split(',') if x.strip())
        elif kind == 'backends':
            if not isinstance(raw, list):
                bad('must be a list of [url, model] pairs')
            value = []
            for item in raw:
                if not (isinstance(item, list) and len(item) == 2):
                    bad(f'must be a list of [url, model] pairs, got {item!r}')
                value.append((_url(item[0]), str(item[1])))
        elif kind == 'presets':
            if not (isinstance(raw, dict) and all(isinstance(v, dict) for v in raw.values())):
                bad('must be a JSON object of objects')
            value = copy.deepcopy(raw)
        else:
            bad(f'unknown setting kind {kind!r}')
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        bad(str(e))
    return value


def _argparser():
    p = argparse.ArgumentParser(add_help=False)
    for name, s in SETTINGS.items():
        if not s.flag:
            continue
        kw = {'dest': name, 'default': None}
        if s.kind in ('int', 'port'):
            kw['type'] = int
        elif s.kind == 'float':
            kw['type'] = float
        if s.flag == '--allow-host':
            p.add_argument(s.flag, action='append', **kw)
        else:
            p.add_argument(s.flag, **kw)
    return p


def mask(url):
    """A secret URL as scheme://[***@]host[:port]/*** -- never the path (the ntfy topic) or
    any userinfo (a token may sit there)."""
    url = str(url or '').strip()
    if not url:
        return ''
    sp = urllib.parse.urlsplit(url)
    host = sp.hostname or ''
    if sp.username or sp.password:
        host = '***@' + host
    port = f':{sp.port}' if sp.port else ''
    return f'{sp.scheme}://{host}{port}/***'


class Config:
    """One resolved configuration: a value and its source ('flag', 'env', 'config.json',
    'default') for every setting."""

    def __init__(self, values, sources, config_file, config_file_exists):
        self.values = values
        self.sources = sources
        self.config_file = config_file
        self.config_file_exists = config_file_exists

    def value(self, name):
        v = self.values[name]
        return copy.deepcopy(v) if isinstance(v, (dict, list)) else v

    def source(self, name):
        return self.sources[name]

    def ws_target(self):
        """(host, port) of ComfyUI's progress websocket, derived from comfy_url."""
        sp = urllib.parse.urlsplit(self.values['comfy_url'])
        return sp.hostname, sp.port or (443 if sp.scheme == 'https' else 80)

    def summary(self):
        """One line: the effective service addresses. Never a secret."""
        host, port = self.ws_target()
        if self.values['director_url']:
            model = self.values['director_model'] or 'default'
            direct = f"director pinned to {self.values['director_url']} ({model})"
        else:
            direct = 'director searching ' + ', '.join(
                urllib.parse.urlsplit(u).netloc for u, _ in self.values['director_backends'])
        return (f"UI http://{self.values['ui_host']}:{self.values['ui_port']}"
                f" · ComfyUI {self.values['comfy_url']} (websocket {host}:{port})"
                f' · {direct}')

    def api_view(self):
        """The effective, secret-free configuration for GET /api/config, with sources."""
        settings = {}
        for name, s in SETTINGS.items():
            v = self.value(name)
            if s.secret:
                v = mask(v)
            elif isinstance(v, Path):
                v = str(v)
            elif isinstance(v, tuple):
                v = list(v)
            settings[name] = {'value': v, 'source': self.source(name), 'secret': s.secret}
        host, port = self.ws_target()
        return {'config_file': {'path': str(self.config_file), 'exists': self.config_file_exists},
                'derived': {'ws_listen': {'value': f'{host}:{port}', 'source': 'derived from comfy_url'}},
                'settings': settings}


def load(argv=None, env=None, config_file=None):
    """Resolve every setting. argv/env/config_file default to the live ones; tests pass their
    own. Raises ConfigError on a malformed file or an invalid value."""
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    path = Path(env.get(CONFIG_FILE_ENV) or (config_file if config_file is not None
                                             else HERE / CONFIG_FILE_NAME))

    data, warnings = {}, []
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except ValueError as e:
            raise ConfigError(f'config: {path} is not valid JSON: {e}') from None
        if not isinstance(data, dict):
            raise ConfigError(f'config: {path} must contain a flat JSON object of settings')
        for key in data:
            if key not in SETTINGS:
                warnings.append(f"config: {path}: ignoring unknown setting {key!r}")
    flags, _ = _argparser().parse_known_args(argv)

    values, sources = {}, {}
    for name, s in SETTINGS.items():
        raw, source = None, 'default'
        if s.flag:
            got = getattr(flags, name, None)
            if got is not None and got != []:
                raw, source = got, f'flag {s.flag}'
        if raw is None and s.env and env.get(s.env) not in (None, ''):
            raw, source = env[s.env], f'env {s.env}'
        if raw is None and data.get(name) is not None:
            raw, source = data[name], str(path)
        values[name] = _parse(name, raw, source) if raw is not None else copy.deepcopy(s.default)
        sources[name] = source
    for w in warnings:
        print(w, file=sys.stderr)
    return Config(values, sources, path, path.is_file())


CFG = load()


def value(name):
    return CFG.value(name)


def source(name):
    return CFG.source(name)


def ws_target():
    return CFG.ws_target()


def summary():
    return CFG.summary()


def api_view():
    return CFG.api_view()


def reload():
    """Re-read argv/env/config.json (tests that change the environment); modules that already
    took a value keep theirs."""
    global CFG
    CFG = load()
    return CFG


def add_service_args(parser, names=SERVICE_FLAGS):
    """Let an entry point advertise the shared service flags (config still resolves them)."""
    for name in names:
        s = SETTINGS[name]
        parser.add_argument(s.flag, dest='cfg_' + name, metavar=name.upper().replace('_', '-'),
                            help=f'override {name} (env {s.env}; default {s.default})')
    return parser
