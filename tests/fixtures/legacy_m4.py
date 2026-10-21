#!/usr/bin/env python3
"""The M4-era (a0928c4) fixture shared by the golden captures and the finishing acceptance suite.

The project is hand-written -- fixed ids, seeds and stamps, legacy take records with no
application id, no 'finishing' key, no stored 'selection' -- so the identical project.json
text loads on the M4 tree (golden captures) and on HEAD. Absolute paths are written with
the ``{{ROOT}}`` placeholder so the committed text is portable across temp dirs.

Nothing here touches the repo's real ``projects/``: callers pass a temp root.
"""
import json
import subprocess
from pathlib import Path

SLUG = 'legacy-m4-film'
CREATED = 1735689600.0

# id, title, seconds, transition, run, take seconds, take fps
SCENES = [
    ('aa11aa', 'Kite in the wind', 5, 'cut', '20250101-000001-kite', 5.0, 24),
    ('bb22bb', 'The field', 4, 'crossfade', '20250102-000002-field', 4.0, 24),
]
PROMPTS = [
    'At 00:01.0, [Shot 1] a red kite climbs over a dusk field, its line taut.',
    'At 00:01.0, [Shot 1] the kite drifts to the left edge of the frame.',
]


def run_dir(root, i):
    """Run directory of scene i (1-based), the same scenes/NN/<run> layout the renderer uses."""
    return Path(root, SLUG, 'scenes', f'{i:02d}', SCENES[i - 1][4])


def take_path(root, i):
    return run_dir(root, i) / 'take.mkv'


def project_dict(root):
    """The legacy project as a plain dict; clip paths point at `root` (build clips there)."""
    root = str(Path(root))
    scenes = []
    for i, (sid, title, seconds, transition, run, _tsec, _tfps) in enumerate(SCENES, 1):
        runp = f'{root}/{SLUG}/scenes/{i:02d}/{run}'
        scenes.append({
            'id': sid, 'title': title, 'seconds': seconds,
            'connect': 'cut' if i == 1 else 'continue', 'action': '',
            'prompt': PROMPTS[i - 1], 'prompt_mode': 't2va', 'seed': 1000 + i,
            'takes': [{'run': run, 'path': f'{runp}/take.mkv',
                       'url': f'/files/{SLUG}/media/{run}.mp4', 'preset': 'draft',
                       'seed': 2000 + i, 'created': CREATED + i, 'final': None,
                       'final_url': None, 'variant': None, 'source_take': None,
                       'thumb_url': f'/files/{SLUG}/thumbs/{run}.jpg'}],
            'star': None, 'selected_take': None, 'transition': transition,
            'history': [], 'prompt_check': None,
        })
    return {'slug': SLUG, 'name': 'Legacy M4 Film', 'logline': 'A kite crosses a dusk field.',
            'seconds': 9, 'aspect': '16:9', 'size': [1024, 576], 'refs': [], 'music_file': None,
            'title': 'Legacy M4 Film', 'bible': 'A kite crosses a dusk field.',
            'music_brief': '', 'notes': '', 'title_card': None, 'end_card': None,
            'scenes': scenes, 'outputs': {}, 'characters': [], 'character_notes': '',
            'character_refs': {}, 'created': CREATED}


def make_take(dst, seconds, fps, tone=220.0, beep=None):
    """Synthetic clip: testsrc2 video + sine audio in Matroska (pcm audio -> exact durations).

    `beep=(start, stop, frequency)` gates a loud sine inside that window and drops the rest
    of the tone to -60 dB, so a decoder can locate the beep's first sample on the finished cut."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['ffmpeg', '-v', 'error', '-y',
           '-f', 'lavfi', '-i', f'testsrc2=size=320x180:rate={fps}:duration={seconds}',
           '-f', 'lavfi', '-i', f'sine=frequency={beep[2] if beep else tone}:duration={seconds}']
    if beep:
        cmd += ['-af', f"volume=volume='if(between(t,{beep[0]},{beep[1]}),1,0.001)':eval=frame"]
    acodec = 'aac' if dst.suffix.lower() in ('.mp4', '.m4v') else 'pcm_s16le'
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', acodec, '-shortest', str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dst


def install(root):
    """Write the fixture's project.json and its take clips under `root`; return the dict."""
    root = Path(root)
    project = project_dict(root)
    for i in range(1, len(SCENES) + 1):
        sid, title, seconds, transition, run, tsec, tfps = SCENES[i - 1]
        make_take(take_path(root, i), tsec, tfps)
    pdir = root / SLUG
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / 'project.json').write_text(json.dumps(project, indent=1))
    return project
