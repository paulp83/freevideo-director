#!/usr/bin/env python3
"""Three separate readiness checks: prompts, video generation, finishing.

The old UI answered one question, "is ComfyUI up?", and treated that as the state of
everything. Writing a prompt needs a director model; rendering needs ComfyUI with the
FreeVideo node set; Enhance/Final/Export need ComfyUI with SeedVR2/RIFE/VHS, their model
files, a writable ComfyUI input/ and an ffmpeg that carries every filter assemble() uses.
Each check is reported on its own: 'ready', 'limited' (something optional is off) or
'unavailable' (a required part is missing). web.py gates each action on its own check and
quotes that check's missing items as the reason; the UI shows three chips for the same.

Everything here is derived, never guessed: the render node classes come from the graph
web.build_graph actually submits (registered through set_render_graph, because readiness
cannot import web), and the finishing node classes and model file names come from
enhance.graph(). A ComfyUI that gains a node set makes its check pass without a change
here.

Probes run in parallel threads with a short per-probe timeout (readiness_timeout); the
result is cached for readiness_cache seconds so a page poll never blocks a request thread
for more than roughly one timeout. A probe never raises: whatever cannot be determined
becomes an item with a name, a plain-words detail and a how_to_fix.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
import director
import enhance
import finishing
import project

CHECKS = ('prompt', 'video', 'finishing')
LABEL = {'prompt': 'Prompts', 'video': 'Video generation', 'finishing': 'Finishing'}


class NotReady(Exception):
    """An action was refused because its readiness check is unavailable."""


def _item(name, ok, short='', detail='', how_to_fix='', hard=False, affects=''):
    return {'name': name, 'ok': bool(ok), 'short': short or name,
            'detail': detail or short or name, 'how_to_fix': how_to_fix,
            'hard': bool(hard), 'affects': affects}


# ---------------------------------------------------------------- HTTP probes

def _base():
    return str(config.value('comfy_url')).rstrip('/')


def _timeout():
    return float(config.value('readiness_timeout'))


def _get_json(path):
    req = urllib.request.Request(_base() + path, headers={'Accept': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=_timeout()).read())


def _comfy_up():
    """One GET: the same answer _comfy_item reports, as a plain bool."""
    try:
        _get_json('/system_stats')
    except urllib.error.HTTPError:
        return True                            # it answered, so it is listening
    except OSError:
        return False
    return True


def _comfy_item(_info=None):
    """One GET: any HTTP answer at all means ComfyUI is listening."""
    try:
        _get_json('/system_stats')
    except urllib.error.HTTPError:
        pass                                  # it answered, so it is up
    except OSError as e:
        return [_item('ComfyUI server', False,
                      short=f'not reachable at {_base()}',
                      detail=f'ComfyUI is not reachable at {_base()} ({e})',
                      how_to_fix='start ComfyUI: python3 main.py --listen 127.0.0.1 --port 8190 '
                                 '(README), or point comfy_url at wherever it runs',
                      hard=True, affects='render, finishing')]
    return [_item('ComfyUI server', True, detail=f'listening at {_base()}')]


def _object_info():
    """The whole /object_info map, or None when it cannot be fetched."""
    try:
        info = _get_json('/object_info')
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def _options(info, cls):
    """Every COMBO option the node class exposes, or None if it exposes none."""
    if not info or cls not in info:
        return None
    found = []
    for level in ('required', 'optional'):
        for spec in (info[cls].get('input') or {}).get(level, {}).values():
            if isinstance(spec, list) and spec and spec[0] == 'COMBO':
                try:
                    found.extend(str(o) for o in spec[1]['options'])
                except (KeyError, TypeError):
                    pass
            elif isinstance(spec, list) and spec and isinstance(spec[0], list):
                found.extend(str(o) for o in spec[0])      # legacy combo: [[names], {default, ...}] or [[names]]
    return found or None


# ---------------------------------------------------------------- prompt check

def _backends():
    """The director backends the UI would use, in the order director.backend() tries them."""
    pinned = str(config.value('director_url') or '')
    if pinned:
        return [(pinned, str(config.value('director_model') or 'default'))]
    return [(u, m) for u, m in director.BACKENDS]


def _addr(url):
    return str(url).replace('/chat/completions', '')


def _director_items(_info=None):
    """The first director backend that answers /models is the one prompts run on."""
    backends = _backends()
    with ThreadPoolExecutor(max_workers=max(1, len(backends))) as pool:
        alive = list(pool.map(lambda b: _model_up(b[0]), backends))
    if not any(alive):
        addrs = ', '.join(_addr(u) for u, _ in backends)
        return [_item('director model', False,
                      short=f'not reachable at {addrs}',
                      detail=f'no director model answers at {addrs}',
                      how_to_fix='start one: docker start director-qwen38-27b or docker start '
                                 'freevideo-director (docs/configuration.md), or set director_url',
                      hard=True, affects='prompt')]
    url, model = next(b for b, up in zip(backends, alive) if up)
    return [_item('director model', True, detail=f'answering at {_addr(url)}'),
            _item('model name', True, detail=f'{model} at {_addr(url)}')]


def _model_up(url):
    try:
        urllib.request.urlopen(url.replace('/chat/completions', '/models'),
                               timeout=_timeout()).close()
        return True
    except OSError:                            # HTTPError (subclass of OSError) counts as down,
        return False                           # exactly like director.backend()


def _skill_items(_info=None):
    """The pinned H3 skill and every reference guide a mode reads before writing a prompt."""
    missing = []
    if not (director.SKILL / 'SKILL.md').is_file():
        missing.append('SKILL.md')
    for guide in sorted({g for g, _label in director.MODES.values()}):
        if not (director.SKILL / 'references' / guide).is_file():
            missing.append(f'references/{guide}')
    if missing:
        return [_item('H3 skill files', False, short='skill files missing',
                      detail=f'{", ".join(missing)} missing under {director.SKILL} — '
                             'the director has no prompt format to write',
                      how_to_fix='restore the pinned MiniMax skill (git checkout skill/)',
                      hard=True, affects='prompt')]
    return [_item('H3 skill files', True, detail='SKILL.md and every mode guide under skill/')]


# ---------------------------------------------------------------- video check

_render_graph = None


def set_render_graph(fn):
    """web.py registers a callable returning the graph build_graph() would submit."""
    global _render_graph
    _render_graph = fn


def _render_classes():
    if not _render_graph:
        return None
    try:
        return sorted({str(n.get('class_type')) for n in _render_graph().values()
                       if isinstance(n, dict)})
    except Exception:
        return None


def _render_items(info):
    classes = _render_classes()
    if not classes:
        return [_item('render node set', False, short='render graph not probed',
                      detail='the render graph could not be enumerated from inside the server',
                      hard=False, affects='render')]
    if info is None:
        return [_item('render node set', False, short='node list not enumerable',
                      detail=f'ComfyUI did not answer /object_info, so {", ".join(classes)} '
                             'could not be confirmed',
                      how_to_fix='start ComfyUI so it can list its nodes',
                      hard=False, affects='render')]
    missing = [c for c in classes if c not in info]
    if missing:
        return [_item('render node set', False,
                      short='FreeVideo nodes not installed',
                      detail=f'{", ".join(missing)} not installed in ComfyUI (affects video generation)',
                      how_to_fix='install the FreeVideo custom nodes the README names into ComfyUI',
                      hard=True, affects='render')]
    return [_item('render node set', True, detail=', '.join(classes))]


def _lora_item(info):
    if info is None:
        return [_item('LoRA picker', False, short='LoRA list not enumerable',
                      detail='ComfyUI did not answer /object_info, so LoRAs cannot be listed',
                      hard=False, affects='render')]
    options = _options(info, 'FreeVideoLoRA')
    if options is None:
        return [_item('LoRA picker', False, short='no LoRA list',
                      detail='this ComfyUI does not list LoRA choices (the picker stays empty)',
                      how_to_fix='install the FreeVideo LoRA node, or render without LoRAs',
                      hard=False, affects='render')]
    return [_item('LoRA picker', True, detail=f'{len(options)} LoRA choices listed')]


def _output_dir_item(_info=None):
    out = Path(enhance.COMFY_ROOT) / 'output'
    if not out.is_dir():
        return [_item('ComfyUI output folder', False, short='output/ missing',
                      detail=f'{out} does not exist, so finished takes cannot be collected',
                      how_to_fix='start ComfyUI once (it creates output/) or point comfy_root '
                                 'at the ComfyUI install',
                      hard=True, affects='render')]
    return [_item('ComfyUI output folder', True, detail=str(out))]


def _which(name):
    return shutil.which(str(config.value(name)))


def _ffmpeg_item(hard, affects, why):
    missing = [name for name in ('ffmpeg', 'ffprobe') if not _which(name)]
    if missing:
        return [_item('ffmpeg', hard,
                      short=f'{" and ".join(missing)} not found',
                      detail=f'{" and ".join(missing)} not found on PATH — needed for {why}',
                      how_to_fix='install ffmpeg (apt install ffmpeg or your distro equivalent)',
                      hard=hard, affects=affects)]
    return [_item('ffmpeg', True, detail=f'{_which("ffmpeg")}, {_which("ffprobe")}')]


# ---------------------------------------------------------------- finishing check

def _finishing_graph():
    try:
        # always the FULL legacy pipeline: a stage a project switched off still gets its
        # dependency probed, so the report can say which setting avoids it
        g = enhance.graph('video.mp4', 'readiness-probe')
        return g if isinstance(g, dict) else None
    except Exception:
        return None


# which finishing switch drops a family's dependency (stage names = _FAMILIES affects)
_STAGE_OFF = {'upscale': ('Upscaling', lambda s: s['upscale']['enabled']),
              'interpolation': ('frame interpolation', lambda s: s['interpolation']['enabled'])}


def _stage_on(settings, stage):
    """Is a family's stage needed by these settings? (No settings asked = every stage.)"""
    off = _STAGE_OFF.get(stage)
    return settings is None or not off or off[1](settings)

_FAMILIES = (
    ('SeedVR2 upscaler not installed', lambda c: 'SEEDVR2' in c.upper(), 'upscale'),
    ('frame interpolation not installed', lambda c: 'RIFE' in c.upper(), 'interpolation'),
    ('video in/out nodes not installed', lambda c: c.upper().startswith('VHS'), 'encode'),
)


def _finishing_nodes_item(info, confirmed=True, settings=None):
    g = _finishing_graph()
    if g is None:
        return [_item('finishing node set', False, short='finishing graph not enumerable',
                      detail='the enhance graph could not be built for probing',
                      hard=False, affects='upscale, interpolation, encode')]
    classes = sorted({str(n.get('class_type')) for n in g.values() if isinstance(n, dict)})
    if info is None:
        return [_item('finishing node set', False, short='node list not enumerable',
                      detail='ComfyUI did not answer /object_info, so the finishing nodes '
                             'could not be confirmed',
                      hard=False, affects='upscale, interpolation, encode')]
    items, known = [], set()
    for short, matches, affects in _FAMILIES:
        fam = [c for c in classes if matches(c)]
        known.update(fam)
        missing = [c for c in fam if c not in info]
        if missing and not _stage_on(settings, affects):
            label = _STAGE_OFF[affects][0]
            items.append(_item(short, True, short=f'{short} not needed',
                                      detail=f'{", ".join(missing)} not installed in ComfyUI, but '
                                      f'{label} is switched off in the finishing settings of '
                                      f'this project, so this project does not need it',
                               how_to_fix=f'install the node pack, or keep {label} switched off '
                                          'in Project settings \u2192 Finishing',
                               affects=affects))
        elif missing:
            label = _STAGE_OFF.get(affects, (None,))[0]
            items.append(_item(f'{short}', False, short=short,
                               detail=f'{", ".join(missing)} not installed in ComfyUI '
                                      f'(affects {affects})',
                               how_to_fix='install the node pack into ComfyUI '
                                          '(ComfyUI-SeedVR2_VideoUpscaler / RIFE / VideoHelperSuite, '
                                          'see README and docs/troubleshooting.md)'
                                          + (f', or turn {label} off in Project settings '
                                             '\u2192 Finishing to drop the dependency' if label else ''),
                               hard=True, affects=affects))
    other = [c for c in classes if c not in known and c not in info]
    if other:
        items.append(_item('finishing node set', False, short='finishing nodes not installed',
                           detail=f'{", ".join(other)} not installed in ComfyUI',
                           how_to_fix='install the custom nodes the README names',
                           hard=True, affects='upscale, interpolation, encode'))
    if len(items) == 0:
        items.append(_item('finishing node set', True, detail=', '.join(classes)))
    return items


def _file_leaves(node, cls, out):
    if isinstance(node, str):
        if node.endswith(('.safetensors', '.pth', '.pt')):
            out.append((cls, node))
    elif isinstance(node, list):
        for v in node:
            _file_leaves(v, cls, out)
    elif isinstance(node, dict):
        for v in node.values():
            _file_leaves(v, cls, out)


def _finishing_files_item(info, confirmed=True, settings=None):
    g = _finishing_graph()
    if g is None:
        return []
    if info is None and not confirmed:
        # the server itself is missing: one item already says so — piling 'not installed'
        # model files next to an unreachable ComfyUI would point at the wrong fix
        return []
    leaves = []
    for node in g.values():
        if isinstance(node, dict):
            _file_leaves(node.get('inputs'), str(node.get('class_type')), leaves)
    items = []
    for cls, fname in leaves:
        stage = ('upscale' if 'SEEDVR2' in cls.upper()
                 else 'interpolation' if 'RIFE' in cls.upper() else 'encode')
        label = _STAGE_OFF.get(stage, (None,))[0]
        if not _stage_on(settings, stage):
            # the stage that would load this model is switched off in this project's
            # settings: the dependency is gone, installed or not, node present or not
            items.append(_item(f'model file {fname}', True, short=f'{fname} not needed',
                               detail=f'{fname} (used by {cls}) only matters to {label}, and '
                                      '{label} is switched off in the finishing settings of '
                                      'this project',
                               how_to_fix=f'install the model, or keep {label} switched off in '
                                          'Project settings → Finishing',
                               affects=stage))
            continue
        options = _options(info, cls) if info is not None else None
        if options is None:
            items.append(_item(f'model file {fname}', False, short=f'{fname} unconfirmed',
                               detail=f'{fname} (used by {cls}) could not be confirmed against '
                                      'the node list',
                               hard=False, affects='upscale, interpolation'))
        elif fname in options:
            items.append(_item(f'model file {fname}', True, detail=f'offered by {cls}'))
        else:
            items.append(_item(f'model file {fname}', False, short=f'{fname} not installed',
                               detail=f'{fname} (used by {cls}) is not among the models that '
                                      'ComfyUI offers for it',
                               how_to_fix='download the model into the folder that node pack '
                                          'reads (docs/troubleshooting.md has the names)'
                                          + (f', or turn {label} off in Project settings '
                                             '\u2192 Finishing to drop the dependency' if label else ''),
                               hard=True, affects='upscale, interpolation'))
    return items


def _input_dir_item(_info=None):
    inp = Path(enhance.COMFY_ROOT) / 'input'
    if not inp.is_dir():
        return [_item('ComfyUI input folder', False, short='input/ missing',
                      detail=f'{inp} does not exist; enhancement writes its work files there',
                      how_to_fix='start ComfyUI once (it creates input/) or point comfy_root '
                                 'at the ComfyUI install',
                      hard=True, affects='upscale, interpolation, encode')]
    if not os.access(inp, os.W_OK):
        return [_item('ComfyUI input folder', False, short='input/ not writable',
                      detail=f'{inp} is not writable by the director server',
                      how_to_fix=f'fix permissions on {inp}',
                      hard=True, affects='upscale, interpolation, encode')]
    return [_item('ComfyUI input folder', True, detail=str(inp))]


_FILTER_LINE = re.compile(r'^\s*[TSC.]{3}\s+([A-Za-z0-9_]+)')


def _ffmpeg_filters():
    """The filter names ffmpeg itself lists, or None when it will not answer."""
    exe = _which('ffmpeg')
    if not exe:
        return None
    try:
        out = subprocess.run([exe, '-hide_banner', '-filters'], capture_output=True,
                             text=True, timeout=_timeout()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = {m.group(1) for line in out.splitlines() if (m := _FILTER_LINE.match(line))}
    return found or None


def _filter_item(_info=None):
    """assemble()/_make_card() need every filter in project.EXPORT_FILTERS."""
    found = _ffmpeg_filters()
    if found is None:
        return [_item('ffmpeg filters for export', False, short='filters unconfirmed',
                      detail='ffmpeg did not list its filters within the probe timeout, so '
                             'the export filter set could not be confirmed',
                      how_to_fix='make sure ffmpeg runs quickly enough (a stalled wrapper '
                                 'or an overloaded machine delays the probe)',
                      hard=False, affects='encode')]
    missing = sorted(project.EXPORT_FILTERS - found)
    if missing:
        return [_item('ffmpeg filters for export', False, short='ffmpeg lacks export filters',
                      detail=f'this ffmpeg lacks {", ".join(missing)} — Enhance/Final/Export '
                             'cannot build the cut with it',
                      how_to_fix='install or upgrade ffmpeg (the filters the README asks for '
                                 'are all in a current build)',
                      hard=True, affects='encode')]
    return [_item('ffmpeg filters for export', True,
                  detail=f'all {len(project.EXPORT_FILTERS)} export filters present')]


def _fallback_item(_info=None):
    return [_item('raw frame fallback', True,
                  detail='enhance reads the raw frames (video.artifacts/rgb.npy + audio.wav) '
                         'while ComfyUI still holds them and falls back to video.mp4 after')]


# ---------------------------------------------------------------- building

def _ffmpeg_item_args(hard, affects, why):
    def probe(_info=None):
        return _ffmpeg_item(hard, affects, why)
    return probe

PROBES = {
    'prompt': ((_director_items, False), (_skill_items, False)),
    'video': ((_comfy_item, 'server'), (_render_items, 'info'), (_lora_item, 'info'),
              (_output_dir_item, 'plain'),
              (_ffmpeg_item_args(False, 'render', 'continuation frames and posters'), 'plain')),
    'finishing': ((_comfy_item, 'server'), (_finishing_nodes_item, 'confirmed'),
                  (_finishing_files_item, 'confirmed'), (_input_dir_item, 'plain'),
                  (_ffmpeg_item_args(True, 'upscale, interpolation, encode',
                                     'the enhance intermediate and the export'), 'plain'),
                  (_filter_item, 'plain'), (_fallback_item, 'plain')),
}




def _guarded(fn, mode, info_fut=None, confirmed_fut=None, settings=None):
    try:
        if mode == 'confirmed':                         # knows whether the server answered
            return fn(info_fut.result(), confirmed_fut.result(), settings=settings)
        if mode == 'info':
            return fn(info_fut.result())
        return fn()
    except Exception as e:                              # a probe never sinks the check
        return [_item('probe', False, short='probe failed',
                      detail=f'the readiness probe {getattr(fn, "__name__", fn)}() raised {e!r}',
                      how_to_fix='report this; the other items still stand', hard=False)]


def _assemble(name, items):
    hard = [i for i in items if not i['ok'] and i['hard']]
    soft = [i for i in items if not i['ok'] and not i['hard']]
    level = 'unavailable' if hard else ('limited' if soft else 'ready')
    if level == 'ready':
        summary = 'ready'
    elif hard:
        summary = 'not ready: ' + '; '.join(i['short'] for i in hard)
    else:
        summary = 'limited: ' + '; '.join(i['short'] for i in soft)
    reason = (f'{LABEL[name]} unavailable: ' + '; '.join(i['detail'] for i in hard)
              if hard else None)
    return {'name': name, 'ok': level != 'unavailable', 'level': level,
            'summary': summary, 'reason': reason, 'items': items,
            'checked_at': time.time()}


def _build(name, settings=None):
    probes = PROBES[name]
    with ThreadPoolExecutor(max_workers=len(probes) + 1) as pool:
        server_fut = pool.submit(_comfy_up) if any(m == 'server' for _fn, m in probes) else None
        info_fut = pool.submit(_object_info) if any(m in ('info', 'confirmed') for _fn, m in probes) else None
        confirmed_fut = (pool.submit(lambda: bool(server_fut.result())) if server_fut and info_fut
                         else None)
        futs = [pool.submit(_guarded, fn, mode, info_fut, confirmed_fut, settings) for fn, mode in probes]
        items = [i for f in futs for i in f.result()]
    return _assemble(name, items)


# ---------------------------------------------------------------- public API

_cache = {}                                    # name -> (monotonic, check dict)
_cache_lock = threading.Lock()


def forget():
    """Drop the cache (tests; and a forced refresh starts fresh)."""
    with _cache_lock:
        _cache.clear()


def check(name, refresh=False, settings=None):
    """One readiness check: {name, ok, level, summary, reason, items, checked_at}.

    `settings` (finishing.py's canonical per-project finishing settings) scores the
    finishing check for ONE project: dependencies of stages those settings switch off are
    reported as satisfied -- and named -- instead of blocking. Without settings every
    stage counts, which is what the header chips and /api/readiness show."""
    if name not in CHECKS:
        raise KeyError(name)
    key = name if settings is None else (name, finishing.settings_key(settings))
    ttl = float(config.value('readiness_cache'))
    if not refresh and ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
    result = _build(name, settings)
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result


def report(settings=None):
    """All three checks, built in parallel so the page poll waits one build, not three.
    Each check is reported on one's own: one unavailable capability says nothing about the
    other two, which is the whole point of the three separate checks. `settings` scores the
    finishing check for one project (see check()); prompt and video never use it."""
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        futs = {n: pool.submit(check, n, False, settings if n == 'finishing' else None)
                for n in CHECKS}
        checks = {n: f.result() for n, f in futs.items()}
    return {'ok': any(c['ok'] for c in checks.values()), 'checks': checks}


def require(*names, settings=None):
    """Raise NotReady(reason) if any check is unavailable; degraded passes through. With
    finishing `settings` the scoring is that project's: a stage it switched off does not
    block the action even if that stage's nodes are missing."""
    for n in names:
        c = check(n, settings=settings)
        if c['level'] == 'unavailable':
            raise NotReady(c['reason'] or f'{LABEL[n]} unavailable')
