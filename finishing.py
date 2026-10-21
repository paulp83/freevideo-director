#!/usr/bin/env python3
"""Project-level finishing settings: everything that happens to a take after it is rendered.

One module owns the finishing pipeline -- SeedVR2 upscale, RIFE frame interpolation, H.264
encode quality -- and the one frame-rate policy that both enhance() and assemble() follow:
either the interpolated rate (source fps x factor) or the original rate. A project with no
stored settings runs the legacy pipeline (upscale on to 1080, interpolation on x2, CRF 16),
exactly what every take and cut made before per-project settings was produced with; the
default values come from config.py, never from here.

Canonical shape -- clean_finishing() accepts partial/partially-legacy payloads and always
returns exactly this:

    {'upscale':       {'enabled': bool, 'height': int},    # SeedVR2 short side
     'interpolation': {'enabled': bool, 'factor': int},    # RIFE multiplier
     'encode':        {'crf': int, 'preset': str | None},  # H.264 quality
     'fps_policy':    'interpolated' | 'original'}         # the one fps decision

`fps_policy` is the frame-rate policy as such; the RIFE multiplier IS the fps factor, so the
policy mirrors `interpolation.enabled`. An explicit policy in an editor payload forces the
switch to match (single source of truth: they can never contradict), and with no explicit
policy the switch decides it.
"""
import config

UPSCALE_HEIGHTS = (720, 1080, 2160)   # SeedVR2 short side; 2160 needs real GPU headroom
INTERP_FACTORS = (2, 4)               # the RIFE node takes any INT >= 1; these are the tested factors
CRF_RANGE = (12, 28)                  # x264 CRF the encoding-quality control accepts
QUALITY_PRESETS = {'high': 16, 'standard': 20, 'small': 24}   # named presets -> CRF
FPS_POLICIES = ('interpolated', 'original')
_SECTIONS = ('upscale', 'interpolation', 'encode')


def defaults():
    """The pipeline as it ran before per-project settings existed (values from config.py)."""
    crf = int(config.value('enhance_crf'))
    return {'upscale': {'enabled': True, 'height': int(config.value('enhance_resolution'))},
            'interpolation': {'enabled': True, 'factor': int(config.value('enhance_multiplier'))},
            'encode': {'crf': crf, 'preset': next((n for n, c in QUALITY_PRESETS.items() if c == crf), None)},
            'fps_policy': 'interpolated'}


def clean_finishing(raw):
    """Validate a finishing payload (None or a partial dict) into the canonical shape.

    Every switch, height, factor, CRF and policy is checked; unknown keys are refused.
    Raises ValueError naming the offending key -- the web layer turns that into a 400."""
    d = defaults()
    if raw is None:
        raw = {}                                       # normalised like any other input
    if not isinstance(raw, dict):
        raise ValueError('finishing must be an object')
    for key in raw:
        if key not in _SECTIONS + ('fps_policy',):
            raise ValueError(f'unknown finishing setting: {key}')
    for section in _SECTIONS:
        val = raw.get(section)
        if val is None:
            continue
        if not isinstance(val, dict):
            raise ValueError(f'finishing {section} must be an object')
        for key in val:
            if key not in d[section]:
                raise ValueError(f'unknown finishing {section} setting: {key}')

    up = dict(d['upscale'])
    if (v := raw.get('upscale')) is not None:
        if 'enabled' in v:
            up['enabled'] = bool(v['enabled'])
        if 'height' in v:
            try:
                up['height'] = int(v['height'])
            except (TypeError, ValueError):
                raise ValueError('finishing upscale height must be a number')
            if up['height'] not in UPSCALE_HEIGHTS:
                raise ValueError(f'finishing upscale height must be one of '
                                 f'{", ".join(map(str, UPSCALE_HEIGHTS))}')
    interp = dict(d['interpolation'])
    if (v := raw.get('interpolation')) is not None:
        if 'enabled' in v:
            interp['enabled'] = bool(v['enabled'])
        if 'factor' in v:
            try:
                interp['factor'] = int(v['factor'])
            except (TypeError, ValueError):
                raise ValueError('finishing interpolation factor must be a number')
            if interp['factor'] not in INTERP_FACTORS:
                raise ValueError(f'finishing interpolation factor must be one of '
                                 f'{", ".join(map(str, INTERP_FACTORS))}')
    enc = dict(d['encode'])
    if (v := raw.get('encode')) is not None:
        if 'preset' in v:
            if v['preset'] not in QUALITY_PRESETS:
                raise ValueError(f'finishing quality preset must be one of '
                                 f'{", ".join(sorted(QUALITY_PRESETS))}')
            enc['crf'] = QUALITY_PRESETS[v['preset']]
            enc['preset'] = v['preset']
        elif 'crf' in v:
            try:
                enc['crf'] = int(v['crf'])
            except (TypeError, ValueError):
                raise ValueError('finishing crf must be a number')
            if not CRF_RANGE[0] <= enc['crf'] <= CRF_RANGE[1]:
                raise ValueError(f'finishing crf must be between {CRF_RANGE[0]} and {CRF_RANGE[1]}')
            enc['preset'] = next((n for n, c in QUALITY_PRESETS.items() if c == enc['crf']), None)

    policy = raw.get('fps_policy')
    if policy is not None:
        if policy not in FPS_POLICIES:
            raise ValueError(f'finishing fps_policy must be one of {", ".join(FPS_POLICIES)}')
        interp['enabled'] = policy == 'interpolated'      # the one fps decision wins
    else:
        policy = 'interpolated' if interp['enabled'] else 'original'
    # one name per CRF, so the defaults and an explicit save of the same values compare equal
    enc['preset'] = next((n for n, c in QUALITY_PRESETS.items() if c == enc['crf']), None)
    return {'upscale': up, 'interpolation': interp, 'encode': enc, 'fps_policy': policy}


def for_project(project):
    """The project's finishing settings, legacy defaults when it has none (never stored)."""
    return clean_finishing(project.get('finishing'))


def is_legacy(settings):
    """True when the settings are exactly the pre-per-project-settings pipeline."""
    return settings == defaults()


def output_fps(source_fps, settings):
    """The one frame-rate decision: the policy says interpolated or original."""
    return source_fps * settings['interpolation']['factor'] \
        if settings['fps_policy'] == 'interpolated' else source_fps


def render_fps():
    """The fps FreeVideo renders takes at (config; the policy multiplies from this)."""
    return int(config.value('enhance_fps'))


def enabled_stages(settings):
    """The finishing stages that actually run, in pipeline order (web job stage keys)."""
    stages = []
    if settings['upscale']['enabled']:
        stages.append('upscale')
    if settings['interpolation']['enabled']:
        stages.append('smooth')
    stages.append('encode')
    return stages


def stage_label(key, settings):
    """A stage label that reflects the settings ('Smooth to 48 fps' only when it is 48)."""
    if key == 'upscale':
        return f"Upscale to {settings['upscale']['height']}p"
    if key == 'smooth':
        return f"Smooth to {output_fps(render_fps(), settings)} fps"
    return 'Encode'


def plan_extras(settings):
    """Extra cut-plan keys that only appear once a project leaves the legacy pipeline; the
    stored fingerprint of a legacy project therefore stays byte-for-byte comparable with
    the plans saved before this feature existed."""
    if is_legacy(settings):
        return {}
    return {'upscale': dict(settings['upscale']), 'interpolation': dict(settings['interpolation'])}


def settings_key(settings):
    """Canonical comparable key of the user's switches (model filenames are config-level)."""
    return (settings['upscale']['enabled'], settings['upscale']['height'],
            settings['interpolation']['enabled'], settings['interpolation']['factor'],
            settings['encode']['crf'], settings['fps_policy'])


def settings_of_snapshot(record):
    """The finishing settings an enhance/render snapshot recorded, canonical, or None.

    New snapshots carry 'settings'; snapshots written before per-project settings recorded
    the legacy pipeline's flat keys (resolution/multiplier/crf) -- those stages all ran,
    that is the recorded truth, not a guess."""
    if not isinstance(record, dict):
        return None
    if 'settings' in record:
        return clean_finishing(record['settings'])
    if not all(k in record for k in ('resolution', 'multiplier', 'crf')):
        return None
    factor = int(record['multiplier'])
    return {'upscale': {'enabled': True, 'height': int(record['resolution'])},
            'interpolation': {'enabled': factor > 1, 'factor': max(factor, 2)},
            'encode': {'crf': int(record['crf']), 'preset': None},
            'fps_policy': 'interpolated' if factor > 1 else 'original'}
