"""Film projects: storyboard -> scenes -> takes -> animatic / film. Storage, continuity and assembly.

A project lives in director/projects/<slug>/ :
  project.json      title, logline, bible, scenes (prompt, seed, takes, star), outputs
  refs/             reference pictures (used as <Picture N> for cut scenes)
  frames/           last frames handed to "continue" scenes (lossless, from the take's rgb.npy)
  music.*           optional music bed for the film
  animatic-*.mp4, film-*.mp4   assembled cuts
Takes stay in ComfyUI's output folder; the project only points at them. Stdlib + ffmpeg.
"""
import json, re, secrets, subprocess, threading, time
from pathlib import Path

import enhance

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'projects'
LOCK = threading.Lock()

SIZES = {'16:9': (1344, 768), '9:16': (768, 1344)}
FILM_SIZES = {'16:9': (1920, 1080), '9:16': (1080, 1920)}
I2VA_LINE = 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'


class ProjectError(Exception):
    pass


# ---------------------------------------------------------------- storage

def slugify(name):
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'film'
    base, n = slug, 2
    while (ROOT / slug).exists():
        slug, n = f'{base}-{n}', n + 1
    return slug


def path(slug, *parts):
    if not re.fullmatch(r'[a-z0-9-]+', slug or ''):
        raise ProjectError('bad project name')
    return ROOT.joinpath(slug, *parts)


def load(slug):
    f = path(slug, 'project.json')
    if not f.is_file():
        raise ProjectError(f'no project {slug!r}')
    return json.loads(f.read_text())


def save(project):
    project['updated'] = time.time()
    f = path(project['slug'], 'project.json')
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(project, indent=1))
    tmp.replace(f)
    return project


def mutate(slug, fn):
    """Load, apply fn(project), save, under the process lock. Returns the project."""
    with LOCK:
        project = load(slug)
        fn(project)
        return save(project)


def listing():
    out = []
    for f in sorted(ROOT.glob('*/project.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            p = json.loads(f.read_text())
        except ValueError:
            continue
        out.append({k: p.get(k) for k in ('slug', 'name', 'title', 'seconds', 'aspect', 'updated')} |
                   {'scenes': len(p.get('scenes', []))})
    return out


def create(name, logline, seconds, aspect, refs):
    """refs: [(filename, bytes)]. Returns the new (un-storyboarded) project."""
    if aspect not in SIZES:
        raise ProjectError('aspect must be 16:9 or 9:16')
    seconds = int(seconds)
    if not 8 <= seconds <= 180:
        raise ProjectError('film length must be 8-180 seconds')
    ROOT.mkdir(parents=True, exist_ok=True)
    slug = slugify(name or logline[:30])
    path(slug, 'refs').mkdir(parents=True)
    path(slug, 'frames').mkdir()
    names = []
    for i, (fname, raw) in enumerate(refs[:2], 1):
        ext = (Path(fname).suffix or '.jpg').lower()
        target = path(slug, 'refs', f'picture-{i}{ext}')
        target.write_bytes(raw)
        names.append(target.name)
    project = {'slug': slug, 'name': name or logline[:40], 'logline': logline, 'seconds': seconds,
               'aspect': aspect, 'size': list(SIZES[aspect]), 'refs': names, 'music_file': None,
               'title': '', 'bible': '', 'music_brief': '', 'scenes': [], 'outputs': {},
               'created': time.time()}
    return save(project)


def new_scene(title='New scene', seconds=6, connect='cut', action=''):
    return {'id': secrets.token_hex(3), 'title': title, 'seconds': int(seconds), 'connect': connect,
            'action': action, 'prompt': '', 'prompt_mode': None, 'seed': secrets.randbelow(2**31),
            'takes': [], 'star': None}


def apply_storyboard(project, plan):
    project['title'] = plan['title']
    project['bible'] = plan['bible']
    project['music_brief'] = plan.get('music', '')
    project['scenes'] = [new_scene(s['title'], s['seconds'], s['connect'], s['action']) for s in plan['scenes']]
    return project


def update_from_editor(project, data):
    """Apply the editor's fields; scenes are matched by id, so takes survive reorder/edits."""
    for key in ('title', 'bible', 'music_brief'):
        if key in data:
            project[key] = str(data[key])
    if 'scenes' in data:
        old = {s['id']: s for s in project['scenes']}
        scenes = []
        for i, item in enumerate(data['scenes']):
            scene = old.get(item.get('id')) or new_scene()
            for key in ('title', 'action', 'prompt'):
                if key in item:
                    scene[key] = str(item[key])
            if 'seconds' in item:
                scene['seconds'] = min(15, max(4, int(item['seconds'])))
            if 'connect' in item:
                scene['connect'] = 'continue' if i and item['connect'] == 'continue' else 'cut'
            scenes.append(scene)
        if scenes:
            scenes[0]['connect'] = 'cut'
        project['scenes'] = scenes
    return project


def scene_index(project, scene_id):
    for i, s in enumerate(project['scenes']):
        if s['id'] == scene_id:
            return i
    raise ProjectError('unknown scene')


def reroll_seed(project, scene_id):
    """Give the scene a fresh seed (never the old one); nothing else changes."""
    scene = project['scenes'][scene_index(project, scene_id)]
    old = scene['seed']
    while scene['seed'] == old:
        scene['seed'] = secrets.randbelow(2**31)
    return project


def chosen(scene):
    """The take used downstream: the starred one, else the latest."""
    takes = scene.get('takes') or []
    for t in takes:
        if t['run'] == scene.get('star'):
            return t
    return takes[-1] if takes else None


def ref_files(project):
    return [path(project['slug'], 'refs', n) for n in project.get('refs', [])]


# ---------------------------------------------------------------- continuity

def last_frame(project, run):
    """Write the take's final frame (lossless, from rgb.npy) to frames/ and return its path."""
    run = enhance.run_dir_of(run)
    npy = run / 'video.artifacts' / 'rgb.npy'
    (frames, height, width, _), offset = enhance.npy_header(npy)
    out = path(project['slug'], 'frames', f'{run.name}-last.png')
    if out.is_file():
        return out
    size = height * width * 3
    with open(npy, 'rb') as f:
        f.seek(offset + (frames - 1) * size)
        raw = f.read(size)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
                    '-i', 'pipe:0', '-frames:v', '1', str(out)], input=raw, check=True)
    return out


def scene_media(project, index):
    """(refs, first) for rendering / prompting scene `index`, as file paths.

    "continue" scenes start on the previous scene's chosen take's last frame (I2VA); everything
    else uses the project's reference pictures (Ref2VA, or T2VA without references)."""
    scene = project['scenes'][index]
    if scene.get('connect') == 'continue' and index:
        prev = chosen(project['scenes'][index - 1])
        if prev:
            return [], last_frame(project, prev['run'])
    return ref_files(project), None


def ensure_i2va(prompt):
    """A continue-scene prompt written before its frame existed lacks the I2VA line; add it."""
    if prompt.lstrip().startswith('For the target video'):
        return prompt
    return I2VA_LINE + '\n\n' + prompt


# ---------------------------------------------------------------- assembly

def _duration(file):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(file)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _has_audio(file):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
                          '-of', 'csv=p=0', str(file)], capture_output=True, text=True).stdout.strip()
    return bool(out)


def assemble(project, kind):
    """Cut the chosen takes into one video. kind: 'animatic' (takes, 24 fps) or 'film' (finals, 48 fps)."""
    clips, missing, not_final = [], [], []
    for i, scene in enumerate(project['scenes'], 1):
        take = chosen(scene)
        if not take:
            missing.append(f'{i} {scene["title"]}')
            continue
        source = take.get('final') if kind == 'film' and take.get('final') else take['path']
        if kind == 'film' and not take.get('final'):
            not_final.append(i)
        clips.append(Path(source))
    if missing:
        raise ProjectError('scenes without a take: ' + ', '.join(missing))
    if not clips:
        raise ProjectError('no scenes')
    w, h = FILM_SIZES[project['aspect']] if kind == 'film' else project['size']
    fps = 48 if kind == 'film' else 24
    cmd, parts, vlabels = ['ffmpeg', '-v', 'error', '-y'], [], []
    total = 0.0
    for i, clip in enumerate(clips):
        cmd += ['-i', str(clip)]
        d = _duration(clip)
        total += d
        parts.append(f'[{i}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p[v{i}]')
        if _has_audio(clip):
            parts.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{d:.3f},'
                         f'afade=t=in:d=0.03,afade=t=out:st={max(0, d - 0.03):.3f}:d=0.03[a{i}]')
        else:
            parts.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[a{i}]')
        vlabels.append(f'[v{i}][a{i}]')
    parts.append(''.join(vlabels) + f'concat=n={len(clips)}:v=1:a=1[v][dia]')
    music = project.get('music_file')
    if music and path(project['slug'], music).is_file():
        cmd += ['-stream_loop', '-1', '-i', str(path(project['slug'], music))]
        m = len(clips)
        fade = min(2.5, total / 4)
        parts += [f'[{m}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume=0.6,'
                  f'afade=t=in:d=0.5,afade=t=out:st={total - fade:.3f}:d={fade:.3f}[mus]',
                  '[dia]asplit=2[dia1][key]',
                  '[mus][key]sidechaincompress=threshold=0.04:ratio=6:attack=20:release=500[duck]',
                  '[dia1][duck]amix=inputs=2:duration=first:normalize=0[mix]',
                  '[mix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]']
    else:
        parts.append('[dia]loudnorm=I=-14:TP=-1.5:LRA=11[aout]')
    out = path(project['slug'], f'{kind}-{time.strftime("%Y%m%d-%H%M%S")}.mp4')
    cmd += ['-filter_complex', ';'.join(parts), '-map', '[v]', '-map', '[aout]',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '16' if kind == 'film' else '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise ProjectError('ffmpeg failed: ' + result.stderr.strip()[-600:])
    return {'file': out.name, 'seconds': round(total, 1), 'scenes': len(clips), 'not_final': not_final,
            'created': time.time()}
