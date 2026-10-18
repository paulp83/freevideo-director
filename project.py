"""Film projects: storyboard -> scenes -> takes -> animatic / film. Storage, continuity and assembly.

A project lives in director/projects/<slug>/ :
  project.json      title, logline, bible, scenes (prompt, seed, takes, star), outputs
  refs/             reference pictures (used as <Picture N> for cut scenes)
  frames/           last frames handed to "continue" scenes (lossless, from the take's rgb.npy)
  music.*           optional music bed for the film
  animatic-*.mp4, film-*.mp4   assembled cuts
Takes stay in ComfyUI's output folder; the project only points at them. Stdlib + ffmpeg.
"""
import json, os, re, secrets, shutil, subprocess, tempfile, threading, time
from pathlib import Path

import enhance
import library

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'projects'
LOCK = threading.Lock()

SIZES = {'16:9': (1344, 768), '9:16': (768, 1344)}
FILM_SIZES = {'16:9': (1920, 1080), '9:16': (1080, 1920)}
I2VA_LINE = 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'
TRANSITIONS = ('cut', 'crossfade', 'dip')
XFADE = 0.5
DIP = 0.4
HISTORY_MAX = 20
EDIT_WINDOW = 300
NOTES_MAX = 1000
CARD_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


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


def recover_interrupted():
    """After a restart: a batch recorded as running died with the old process; mark it so."""
    for f in ROOT.glob('*/project.json'):
        try:
            last = json.loads(f.read_text()).get('last_batch')
        except ValueError:
            continue
        if last and not last.get('done'):
            mutate(f.parent.name, lambda p: p['last_batch'].update(
                done=True, error='interrupted by restart', finished=time.time()))


def create(name, logline, seconds, aspect, refs, characters=()):
    """refs: [(filename, bytes)]. Returns the new (un-storyboarded) project."""
    if aspect not in SIZES:
        raise ProjectError('aspect must be 16:9 or 9:16')
    seconds = int(seconds)
    if not 8 <= seconds <= 180:
        raise ProjectError('film length must be 8-180 seconds')
    if not isinstance(characters, (list, tuple)) or not all(isinstance(c, str) for c in characters):
        raise ProjectError('characters must be a list of library names')
    chars, seen = [], []
    for slug in characters:
        if slug in seen:
            continue
        seen.append(slug)
        try:
            chars.append(library.get(slug))
        except library.LibraryError:
            raise ProjectError(f'no such character: {slug}')
    pics = [c for c in chars if c.get('image')]
    if pics and len(refs) + len(pics) > 2:
        raise ProjectError('at most 2 reference pictures, including library characters')
    sources = {}
    for c in pics:                                 # resolve the files now: nothing exists yet if one is missing
        try:
            sources[c['slug']] = library.image_path(c['slug'])
        except library.LibraryError as e:
            raise ProjectError(f'picture of {c["name"]} is unavailable: {e}')
        if not sources[c['slug']].is_file():
            raise ProjectError(f'picture of {c["name"]} is missing')
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
    for c in chars:
        if not c.get('image'):
            continue
        src = sources[c['slug']]
        n = len(names) + 1
        target = path(slug, 'refs', f'picture-{n}{src.suffix}')
        target.write_bytes(src.read_bytes())
        c['picture'] = n
        names.append(target.name)
    notes = []
    for c in chars:
        line = f'{c["name"]}: {c["description"]}'
        if c.get('picture'):
            line += f' (see <Picture {c["picture"]}>)'
        notes.append(line)
    project = {'slug': slug, 'name': name or logline[:40], 'logline': logline, 'seconds': seconds,
               'aspect': aspect, 'size': list(SIZES[aspect]), 'refs': names, 'music_file': None,
               'title': '', 'bible': '', 'music_brief': '', 'notes': '', 'title_card': None, 'end_card': None,
               'scenes': [], 'outputs': {}, 'characters': [c['slug'] for c in chars],
               'character_notes': chr(10).join(notes), 'created': time.time()}
    return save(project)


def new_scene(title='New scene', seconds=6, connect='cut', action=''):
    return {'id': secrets.token_hex(3), 'title': title, 'seconds': int(seconds), 'connect': connect,
            'action': action, 'prompt': '', 'prompt_mode': None, 'seed': secrets.randbelow(2**31),
            'takes': [], 'star': None, 'transition': 'cut', 'history': [], 'prompt_check': None}


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
    if 'notes' in data:
        if not isinstance(data['notes'], str) or len(data['notes']) > NOTES_MAX:
            raise ProjectError('notes must be text of at most 1000 characters')
        project['notes'] = data['notes']
    if 'scenes' in data:
        old = {s['id']: s for s in project['scenes']}
        scenes = []
        for i, item in enumerate(data['scenes']):
            scene = old.get(item.get('id')) or new_scene()
            for key in ('title', 'action'):
                if key in item:
                    scene[key] = str(item[key])
            if 'prompt' in item:
                new = str(item['prompt'])
                if new != scene.get('prompt', ''):
                    if scene.get('prompt', '').strip() and not scene.get('history'):
                        push_history(scene, scene['prompt'], scene.get('prompt_mode'), 'edit')
                    h = scene.get('history') or []
                    if new.strip() and h and h[-1].get('live') and h[-1]['prompt'] == scene.get('prompt', '') and time.time() - h[-1]['time'] < EDIT_WINDOW:
                        h[-1].update(prompt=new, time=time.time())  # still typing, update the entry in place
                    else:
                        last = h[-1] if h else None
                        push_history(scene, new, scene.get('prompt_mode'), 'edit')
                        if scene.get('history') and scene['history'][-1] is not last:
                            scene['history'][-1]['live'] = True
                    scene['prompt'] = new
                    scene['prompt_check'] = None
            if 'seconds' in item:
                scene['seconds'] = min(15, max(4, int(item['seconds'])))
            if 'connect' in item:
                scene['connect'] = 'continue' if i and item['connect'] == 'continue' else 'cut'
            if 'transition' in item:
                if item['transition'] not in TRANSITIONS:
                    raise ProjectError('transition must be cut, crossfade or dip')
                scene['transition'] = item['transition'] if i else 'cut'
            scenes.append(scene)
        if scenes:
            scenes[0]['connect'] = 'cut'
        project['scenes'] = scenes
    for key in ('title_card', 'end_card'):
        if key in data:
            project[key] = _clean_card(data[key])
    if 'loras' in data:
        project['loras'] = clean_loras(data['loras'])
    return project


def clean_loras(items):
    """Validate a film's LoRA list -> [{'name', 'strength', 'enabled'}]: at most 8, .safetensors names, strength -2..2."""
    if not isinstance(items, list) or len(items) > 8:
        raise ProjectError('loras must be a list of at most 8 items')
    out = []
    for item in items:
        name = item.get('name') if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.endswith('.safetensors') or '/' in name or '..' in name:
            raise ProjectError('lora name must be a .safetensors file name')
        strength = item.get('strength', 1.0)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not -2 <= strength <= 2:
            raise ProjectError('lora strength must be a number from -2 to 2')
        enabled = item.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ProjectError('lora enabled must be true or false')
        out.append({'name': name, 'strength': float(strength), 'enabled': enabled})
    return out


def _clean_card(card):
    """Validate an editor card: None or empty text clears it; text 1-80 chars, seconds 1-10."""
    if card is None:
        return None
    if not isinstance(card, dict):
        raise ProjectError('card must be an object')
    text = card.get('text')
    if not isinstance(text, str):
        raise ProjectError('card text must be a string')
    if not text:
        return None
    if not 1 <= len(text) <= 80:
        raise ProjectError('card text must be 1-80 characters')
    seconds = card.get('seconds')
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 10:
        raise ProjectError('card seconds must be 1-10')
    return {'text': text, 'seconds': seconds}


def push_history(scene, prompt, mode, source):
    """Record a prompt in the scene's history; deduped against the latest entry, capped."""
    if not isinstance(prompt, str) or not prompt.strip():
        return
    h = scene.setdefault('history', [])
    if h and h[-1]['prompt'] == prompt:
        return
    h.append({'prompt': prompt, 'mode': mode, 'time': time.time(), 'source': source})
    del h[:-HISTORY_MAX]


def restore_prompt(project, scene_id, index):
    """Restore history entry `index`: current prompt pushed first, prompt_check cleared."""
    scene = project['scenes'][scene_index(project, scene_id)]
    h = scene.get('history') or []
    if type(index) is not int or not 0 <= index < len(h):
        raise ProjectError('no such history entry')
    entry = h[index]
    push_history(scene, scene.get('prompt', ''), scene.get('prompt_mode'), 'edit')
    scene['prompt'], scene['prompt_mode'] = entry['prompt'], entry['mode']
    scene['prompt_check'] = None
    return project


TRASH_NAME = '.director-trash'


def _tree_bytes(path):
    """Size in bytes of a file or of everything under a directory (0 when it is gone)."""
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, names in os.walk(path):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
            except OSError:
                pass
    return total


def _inside(path, root):
    """True when `path` is strictly inside `root` (both resolved)."""
    return Path(root).resolve() in Path(path).resolve().parents


def prune_plan(project, trash_root=None):
    """What prune_apply would move: unstarred takes of scenes that have a valid star. Changes nothing."""
    out = enhance.COMFY_ROOT / 'output'
    trash = Path(trash_root or out / TRASH_NAME).resolve()
    candidates, skipped, seen = [], [], set()
    for scene in project['scenes']:
        star, takes = scene.get('star'), scene.get('takes') or []
        if not star or not any(t['run'] == star for t in takes):
            continue
        star_path = Path(star).resolve()
        for t in takes:
            run = Path(t['run']).resolve()
            if run == star_path or run in seen:
                continue
            seen.add(run)
            if not _inside(run, out) or run == trash or trash in run.parents:
                skipped.append({'scene': scene['id'], 'run': t['run'], 'reason': 'outside the output folder'})
                continue
            final, final_bytes = t.get('final'), 0
            if final and Path(final).is_file() and _inside(final, out) and trash not in Path(final).resolve().parents:
                final_bytes = _tree_bytes(final)
            candidates.append({'scene': scene['id'], 'run': t['run'], 'bytes': _tree_bytes(run), 'final_bytes': final_bytes})
    return {'candidates': candidates, 'skipped': skipped,
            'total_bytes': sum(c['bytes'] + c['final_bytes'] for c in candidates)}


def prune_apply(project, trash_root=None):
    """Move the planned takes (run dir + final file) into <trash>/<stamp>/ and drop them from `takes`. Never deletes."""
    out = enhance.COMFY_ROOT / 'output'
    trash_root = Path(trash_root or out / TRASH_NAME)
    plan = prune_plan(project, trash_root)
    stamp = trash_root / time.strftime('%Y%m%d-%H%M%S')
    moved = total = 0
    for k, c in enumerate(plan['candidates'], 1):
        scene = project['scenes'][scene_index(project, c['scene'])]
        take = next(t for t in scene['takes'] if t['run'] == c['run'])
        run = Path(c['run'])
        if run.exists():
            stamp.mkdir(parents=True, exist_ok=True)
            shutil.move(str(run), str(stamp / f'{k}-{run.name}'))
        if c['final_bytes']:
            (stamp / 'finals').mkdir(parents=True, exist_ok=True)
            shutil.move(take['final'], str(stamp / 'finals' / f'{k}-{Path(take["final"]).name}'))
        scene['takes'] = [t for t in scene['takes'] if t['run'] != c['run']]
        moved += 1
        total += c['bytes'] + c['final_bytes']
    return {'moved': moved, 'bytes': total, 'trash': str(stamp), 'skipped': plan['skipped']}


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
    """Write the take's final frame (lossless from rgb.npy; from video.mp4 when that is gone) to frames/ and return its path."""
    run = enhance.run_dir_of(run)
    npy = run / 'video.artifacts' / 'rgb.npy'
    out = path(project['slug'], 'frames', f'{run.name}-last.png')
    if out.is_file():
        return out
    if not npy.is_file():                         # FreeVideo 0.4.9+ frees the raw frames: take it from the MP4
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-sseof', '-0.5', '-i', str(run / 'video.mp4'),
                        '-update', '1', str(out)], check=True)
        return out
    (frames, height, width, _), offset = enhance.npy_header(npy)
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


def _make_card(card, w, h, fps, tmpdir, name):
    """Render one black card with centred white text and silent stereo audio; return its mp4 path."""
    seconds = float(card['seconds'])
    s = f'{seconds:g}'
    txt = Path(tmpdir, f'{name}.txt')
    txt.write_text(card['text'])
    fs = max(16, min(h // 12, int(1.3 * w / len(card['text']))))
    alpha = f'if(lt(t,0.5),t/0.5,if(gt(t,{s}-0.5),({s}-t)/0.5,1))'
    out = Path(tmpdir, f'{name}.mp4')
    cmd = ['ffmpeg', '-v', 'error', '-y',
           '-f', 'lavfi', '-i', f'color=c=black:s={w}x{h}:r={fps}:d={s}',
           '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
           '-filter_complex',
           f'[0:v]drawtext=fontfile={CARD_FONT}:textfile={txt}:expansion=none:fontcolor=white:'
           f"fontsize={fs}:x=(w-text_w)/2:y=(h-text_h)/2:alpha='{alpha}'[v]",
           '-map', '[v]', '-map', '1:a',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
           '-t', s, '-shortest', str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise ProjectError('ffmpeg failed: ' + result.stderr.strip()[-600:])
    return out


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
    n_scenes, cards = len(clips), None
    trans = [s.get('transition', 'cut') for s in project['scenes']]
    if project.get('title_card') or project.get('end_card'):
        cards = tempfile.TemporaryDirectory()
        if project.get('title_card'):
            clips.insert(0, _make_card(project['title_card'], w, h, fps, cards.name, 'title'))
            trans.insert(0, 'cut')
        if project.get('end_card'):
            clips.append(_make_card(project['end_card'], w, h, fps, cards.name, 'end'))
            trans.append('cut')
    cmd, parts, dur = ['ffmpeg', '-v', 'error', '-y'], [], []
    for i, clip in enumerate(clips):
        cmd += ['-i', str(clip)]
        d = _duration(clip)
        dur.append(d)
        parts.append(f'[{i}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p[v{i}]')
        if _has_audio(clip):
            parts.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{d:.3f},'
                         f'afade=t=in:d=0.03,afade=t=out:st={max(0, d - 0.03):.3f}:d=0.03[a{i}]')
        else:
            parts.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[a{i}]')
    vcur, acur, tcur = 'v0', 'a0', dur[0]
    for i in range(1, len(clips)):
        d, tr = dur[i], trans[i]
        if tr == 'crossfade':
            parts += [f'[{vcur}]settb=AVTB[m{i}]', f'[v{i}]settb=AVTB[n{i}]',
                      f'[m{i}][n{i}]xfade=transition=fade:duration={XFADE}:offset={tcur - XFADE:.3f}[x{i}]']
            parts.append(f'[{acur}][a{i}]acrossfade=d={XFADE}[y{i}]')
            vcur, acur, tcur = f'x{i}', f'y{i}', tcur + d - XFADE
        elif tr == 'dip':
            parts += [f'[{vcur}]fade=t=out:st={tcur - DIP:.3f}:d={DIP}:color=black[p{i}]',
                      f'[v{i}]fade=t=in:st=0:d={DIP}:color=black[q{i}]',
                      f'[{acur}]afade=t=out:st={tcur - DIP:.3f}:d={DIP}[r{i}]',
                      f'[a{i}]afade=t=in:st=0:d={DIP}[s{i}]',
                      f'[p{i}][q{i}]concat=n=2:v=1:a=0[x{i}]',
                      f'[r{i}][s{i}]concat=n=2:v=0:a=1[y{i}]']
            vcur, acur, tcur = f'x{i}', f'y{i}', tcur + d
        else:
            parts.append(f'[{vcur}][v{i}]concat=n=2:v=1:a=0[x{i}]')
            parts.append(f'[{acur}][a{i}]concat=n=2:v=0:a=1[y{i}]')
            vcur, acur, tcur = f'x{i}', f'y{i}', tcur + d
    music = project.get('music_file')
    if music and path(project['slug'], music).is_file():
        cmd += ['-stream_loop', '-1', '-i', str(path(project['slug'], music))]
        m = len(clips)
        fade = min(2.5, tcur / 4)
        parts += [f'[{m}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{tcur:.3f},volume=0.6,'
                  f'afade=t=in:d=0.5,afade=t=out:st={tcur - fade:.3f}:d={fade:.3f}[mus]',
                  f'[{acur}]asplit=2[dia1][key]',
                  '[mus][key]sidechaincompress=threshold=0.04:ratio=6:attack=20:release=500[duck]',
                  '[dia1][duck]amix=inputs=2:duration=first:normalize=0[mix]',
                  '[mix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]']
    else:
        parts.append(f'[{acur}]loudnorm=I=-14:TP=-1.5:LRA=11[aout]')
    out = path(project['slug'], f'{kind}-{time.strftime("%Y%m%d-%H%M%S")}.mp4')
    cmd += ['-filter_complex', ';'.join(parts), '-map', f'[{vcur}]', '-map', '[aout]',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '16' if kind == 'film' else '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if cards:
        cards.cleanup()
    if result.returncode:
        raise ProjectError('ffmpeg failed: ' + result.stderr.strip()[-600:])
    return {'file': out.name, 'seconds': round(tcur, 1), 'scenes': n_scenes, 'not_final': not_final,
            'created': time.time()}
