"""Film projects: storyboard -> scenes -> takes -> animatic / film. Storage, continuity and assembly.

A project lives in director/projects/<slug>/ :
  project.json      title, logline, bible, scenes (prompt, seed, takes, star), outputs, rev + key_revs
  refs/             reference pictures (used as <Picture N> for cut scenes)
  frames/           last frames handed to "continue" scenes (lossless, from the take's rgb.npy)
  jobs/             one immutable request snapshot <job id>.json per render/enhance job, plus <job id>.result.json
  refs-store/       content-addressed copies (<sha256><ext>) of every input picture a job used
  music.*           optional music bed for the film
  animatic-*.mp4, film-*.mp4   assembled cuts
Takes stay in ComfyUI's output folder; the project only points at them. Stdlib + ffmpeg.
"""
import copy, hashlib, json, os, re, secrets, shutil, subprocess, tempfile, threading, time
from pathlib import Path

import config
import enhance
import finishing
import library

HERE = Path(__file__).resolve().parent
ROOT = config.value('projects_root')   # --projects-root / PROJECTS_ROOT / config.json
LOCK = threading.Lock()

SIZES = {'16:9': (1344, 768), '9:16': (768, 1344)}
FILM_SIZES = {'16:9': (1920, 1080), '9:16': (1080, 1920)}
I2VA_LINE = 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'
MAX_REFS = 4                                  # reference pictures per film, uploads and library characters together
TRANSITIONS = ('cut', 'crossfade', 'dip')
XFADE = config.value('xfade_seconds')
DIP = config.value('dip_seconds')
FFMPEG = config.value('ffmpeg')
FFPROBE = config.value('ffprobe')
# the ffmpeg filters assemble() and _make_card() emit; readiness.py checks an ffmpeg that
# answers for all of them before offering Enhance / Final / Export
EXPORT_FILTERS = frozenset('''acrossfade afade aformat anullsrc apad aresample asplit atrim
                            concat drawtext fade format fps loudnorm scale setsar settb
                            sidechaincompress volume xfade'''.split())
# The animatic keeps a draft export policy of its own; the film cut's fps and crf come from
# the project's finishing settings (finishing.py) -- one fps policy, not a second constant.
PREVIEW_FPS = config.value('preview_export_fps')      # the encoder flags assemble() runs
PREVIEW_CRF = config.value('preview_export_crf')      # with for the animatic; cut_plan()
                                                      # reports them in the export spec
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
    """Load, apply fn(project), save, under the process lock; bumps `rev`. Returns the project."""
    with LOCK:
        return _mutate(slug, fn)


def _mutate(slug, fn):
    """mutate() without the lock: callers that must check and write as one step hold LOCK themselves."""
    project = load(slug)
    migrate_selections(project)            # a bare `star` folds into the selection fields, on write only
    before = copy.deepcopy(_flat(project))       # fn edits nested lists/dicts in place, so snapshot
    fn(project)
    _stamp(project, before)
    return save(project)


# ---------------------------------------------------------------- revisions
#
# Every write through mutate() bumps the project's integer `rev` and stamps each field it changed in
# `key_revs` (field -> the rev that field last changed). An editor save may carry the `rev` it was
# loaded at as `base_rev`: when none of the fields that save touches changed since then the patch merges
# on its own, otherwise the save is refused with HTTP 409 plus the current project and the client keeps
# its draft. Server-side writes (recorded takes, stars, seeds, batch state) never consult base_rev,
# they only bump `rev`. Projects written before revisions have no `rev` key: rev_of() reads 0.

BOOKKEEPING = ('rev', 'key_revs', 'updated')
EDITOR_FIELDS = ('title', 'bible', 'music_brief', 'notes', 'title_card', 'end_card', 'loras',
                 'finishing', 'scenes')
SCENE_FIELDS = ('title', 'action', 'prompt', 'seconds', 'connect', 'transition')
_MISSING = object()


def rev_of(project):
    return int(project.get('rev') or 0)


def _flat(project):
    """The fields that get a rev stamp, as field -> value. Scenes are tracked per scene per field, so a
    take recorded on one scene does not collide with an edit of another; the `scenes` field itself covers
    only the order and identity of the list (reorder, add, remove)."""
    out = {k: v for k, v in project.items() if k not in BOOKKEEPING}
    out['scenes'] = [s.get('id') for s in project.get('scenes', [])]
    for scene in project.get('scenes', []):
        for key, value in scene.items():
            out[f'scenes.{scene.get("id")}.{key}'] = value
    return out


def _stamp(project, before):
    """Bump `rev` and record in `key_revs` the fields that differ from the `before` snapshot."""
    rev = rev_of(project) + 1
    project['rev'] = rev
    key_revs = dict(project.get('key_revs') or {})
    for field, value in _flat(project).items():
        if before.get(field, _MISSING) != value:
            key_revs[field] = rev
    alive = {s.get('id') for s in project.get('scenes', [])}
    project['key_revs'] = {f: r for f, r in key_revs.items()
                           if not (f.startswith('scenes.') and len(p := f.split('.')) == 3 and p[1] not in alive)}


def patch_fields(data):
    """The fields an editor patch would touch, named the way `key_revs` names them."""
    fields = {key for key in EDITOR_FIELDS if key in data}
    for item in data.get('scenes') or []:
        if isinstance(item, dict) and item.get('id'):
            fields |= {f"scenes.{item['id']}.{key}" for key in SCENE_FIELDS if key in item}
    return fields


def conflicts(project, base_rev, data):
    """The patch fields that changed after base_rev; empty when the patch merges cleanly. A base_rev that
    is not an integer in 0..rev is a conflict whatever the patch touches. A delta patch naming a scene the
    server no longer has conflicts on `scenes` even when the id-list stamp alone would say otherwise."""
    if type(base_rev) is not int or not 0 <= base_rev <= rev_of(project):
        return sorted(patch_fields(data)) or ['rev']
    key_revs = project.get('key_revs') or {}
    fields = {f for f in patch_fields(data) if int(key_revs.get(f, 0)) > base_rev}
    if data.get('scenes_delta'):
        alive = {s.get('id') for s in project.get('scenes', [])}
        if any(isinstance(item, dict) and item.get('id') not in alive for item in data.get('scenes') or []):
            fields.add('scenes')
    return sorted(fields)


def save_editor(slug, data, base_rev=None):
    """Apply the editor's patch, returning (project, fields). With base_rev the patch merges only when it
    conflicts on nothing; otherwise nothing is written and `fields` names what got in the way."""
    if base_rev is None:
        return mutate(slug, lambda p: update_from_editor(p, data)), []
    with LOCK:                                   # the check and the write are one step
        project = load(slug)
        fields = conflicts(project, base_rev, data)
        if fields:
            return None, fields
        return _mutate(slug, lambda p: update_from_editor(p, data)), []


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
    if pics and len(refs) + len(pics) > MAX_REFS:
        raise ProjectError(f'at most {MAX_REFS} reference pictures, including library characters')
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
    names, cref = [], {}
    for i, (fname, raw) in enumerate(refs[:MAX_REFS], 1):
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
        cref[c['slug']] = target.name
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
               'character_notes': chr(10).join(notes), 'character_refs': cref, 'created': time.time()}
    return save(project)


def set_characters(project, slugs):
    """Replace the film's library characters: their pictures in refs/ (uploads stay) and the character notes.

    Scene prompts already written are not touched; rewrite them to mention the new characters."""
    if not isinstance(slugs, list) or not all(isinstance(c, str) for c in slugs):
        raise ProjectError('characters must be a list of library names')
    chars = []
    for slug in dict.fromkeys(slugs):
        try:
            chars.append(library.get(slug))
        except library.LibraryError:
            raise ProjectError(f'no such character: {slug}')
    old = project.get('character_refs')
    if old is None:                                # made before character_refs: the library pictures came last
        pic_slugs = []
        for slug in project.get('characters', []):
            try:
                if library.get(slug).get('image'):
                    pic_slugs.append(slug)
            except library.LibraryError:
                pass
        old = dict(zip(pic_slugs, project['refs'][len(project['refs']) - len(pic_slugs):])) if pic_slugs else {}
    uploaded = [n for n in project['refs'] if n not in old.values()]
    pics = [c for c in chars if c.get('image')]
    if len(uploaded) + len(pics) > MAX_REFS:
        raise ProjectError(f'at most {MAX_REFS} reference pictures, including library characters')
    sources = {}
    for c in pics:
        try:
            sources[c['slug']] = library.image_path(c['slug'])
        except library.LibraryError as e:
            raise ProjectError(f'picture of {c["name"]} is unavailable: {e}')
        if not sources[c['slug']].is_file():
            raise ProjectError(f'picture of {c["name"]} is missing')
    for name in old.values():
        path(project['slug'], 'refs', name).unlink(missing_ok=True)
    names, cref = list(uploaded), {}
    for c in pics:
        src = sources[c['slug']]
        target = path(project['slug'], 'refs', f'character-{c["slug"]}{src.suffix}')
        target.write_bytes(src.read_bytes())
        names.append(target.name)
        c['picture'] = len(names)
        cref[c['slug']] = target.name
    notes = []
    for c in chars:
        line = f'{c["name"]}: {c["description"]}'
        if c.get('picture'):
            line += f' (see <Picture {c["picture"]}>)'
        notes.append(line)
    project.update(refs=names, characters=[c['slug'] for c in chars], character_refs=cref,
                   character_notes=chr(10).join(notes))
    return project


def new_scene(title='New scene', seconds=6, connect='cut', action=''):
    return {'id': secrets.token_hex(3), 'title': title, 'seconds': int(seconds), 'connect': connect,
            'action': action, 'prompt': '', 'prompt_mode': None, 'seed': secrets.randbelow(2**31),
            'takes': [], 'star': None, 'selected_take': None, 'selection': 'auto',
            'transition': 'cut', 'history': [], 'prompt_check': None}


def apply_storyboard(project, plan):
    project['title'] = plan['title']
    project['bible'] = plan['bible']
    project['music_brief'] = plan.get('music', '')
    project['scenes'] = [new_scene(s['title'], s['seconds'], s['connect'], s['action']) for s in plan['scenes']]
    return project


def _apply_scene_fields(scene, item, i):
    """Apply one scene patch's fields under the editor's rules; `i` is the scene's position, which decides
    whether a continue connection or a non-cut transition survives (the first scene never has either)."""
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


def update_from_editor(project, data):
    """Apply the editor's fields; scenes are matched by id, so takes survive reorder/edits. A
    `scenes_delta` patch carries only the scenes the user touched: everything else on the server --
    recorded takes included -- is left exactly as it is."""
    for key in ('title', 'bible', 'music_brief'):
        if key in data:
            project[key] = str(data[key])
    if 'notes' in data:
        if not isinstance(data['notes'], str) or len(data['notes']) > NOTES_MAX:
            raise ProjectError('notes must be text of at most 1000 characters')
        project['notes'] = data['notes']
    if 'scenes' in data:
        if data.get('scenes_delta'):
            old = {s['id']: s for s in project['scenes']}
            for item in data['scenes']:
                scene = old.get(item.get('id'))
                if scene is None:
                    raise ProjectError('a patched scene no longer exists on the server')   # races lose to conflicts()
                pos = next(k for k, s in enumerate(project['scenes']) if s is scene)
                _apply_scene_fields(scene, item, pos)
        else:
            old = {s['id']: s for s in project['scenes']}
            scenes = []
            for i, item in enumerate(data['scenes']):
                scenes.append(old.get(item.get('id')) or new_scene())
                _apply_scene_fields(scenes[-1], item, i)
            if scenes:
                scenes[0]['connect'] = 'cut'
            project['scenes'] = scenes
    for key in ('title_card', 'end_card'):
        if key in data:
            project[key] = _clean_card(data[key])
    if 'loras' in data:
        project['loras'] = clean_loras(data['loras'])
    if 'finishing' in data:
        try:
            project['finishing'] = finishing.clean_finishing(data['finishing'])
        except ValueError as e:
            raise ProjectError(str(e))
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
    """What prune_apply would move: unstarred takes of scenes that have a valid star. Changes nothing.
    Contents of the project's jobs/ and refs-store/ are never moveable: a generation record and the
    input copies its hashes point at outlive every take they describe."""
    candidates, skipped, seen = [], [], set()
    records = [path(project['slug'], d).resolve() for d in (JOBS_DIR, REFS_STORE)]
    out = enhance.COMFY_ROOT / 'output'
    trash = Path(trash_root or out / TRASH_NAME).resolve()
    for scene in project['scenes']:
        star, takes = scene.get('star'), scene.get('takes') or []
        if not star or not any(t['run'] == star for t in takes):
            continue
        star_path = Path(star).resolve()
        for t in takes:
            run = Path(t['run']).resolve()
            if run == star_path or run in seen:
                continue
            if any(run == root or root in run.parents for root in records):
                skipped.append({'scene': scene['id'], 'run': t['run'], 'reason': 'kept: generation record'})
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


# ---------------------------------------------------------------- selection
#
# A scene resolves downstream in one of two ways: an *explicit* pick (`selection: 'explicit'`,
# `selected_take` = the take's application id, or the run directory for a legacy take that has no
# id) or the *automatic* latest-take fallback (`selection: 'auto'`). `star` is the pre-selection
# spelling of a pick: it stays readable, old projects keep loading untouched, and
# migrate_selections() folds it into the new fields the first time the project is written -- never
# on load, so GETs leave old files byte for byte as they are. A newly finished take never moves an
# explicit pick; set_auto() hands a scene back to following its latest take.

def take_ref(take):
    """How a scene addresses a take: its application id, or the run directory when it is legacy."""
    return take.get('id') or take['run']


def find_take(scene, ref):
    """The take of `scene` whose id or run is `ref`, or None."""
    for t in scene.get('takes') or []:
        if ref and ref in (t.get('id'), t.get('run')):
            return t
    return None


def selection_of(scene):
    """The scene's effective (selected_take, selection). A bare `star` reads as an explicit pick
    without touching the file; when a pick points at a take that is gone, chosen() falls back."""
    if scene.get('selection') == 'explicit':
        return scene.get('selected_take'), 'explicit'
    if scene.get('selection') == 'auto':
        return None, 'auto'
    if scene.get('star'):                        # pre-selection project: a star was a manual pick
        return scene['star'], 'explicit'
    return None, 'auto'


def migrate_selections(project):
    """Fold each scene's bare `star` into selected_take/selection. Write-path only (see _mutate)."""
    for scene in project.get('scenes', []):
        if 'selection' in scene:
            continue
        star = scene.get('star')
        take = find_take(scene, star)
        scene['selected_take'] = take_ref(take) if take else (star or None)
        scene['selection'] = 'explicit' if star else 'auto'


def set_explicit(project, scene_id, ref):
    """Pin the scene to one take (application id, or run for a legacy take); `star` keeps in step."""
    scene = project['scenes'][scene_index(project, scene_id)]
    take = find_take(scene, ref)
    if not take:
        raise ProjectError(f'no take {ref!r} in this scene')
    scene['selected_take'] = take_ref(take)
    scene['selection'] = 'explicit'
    scene['star'] = take['run']                  # old readers (prune, pages) keep working
    return project


def set_auto(project, scene_id):
    """Release the pick: the scene follows its latest take again."""
    scene = project['scenes'][scene_index(project, scene_id)]
    scene['selected_take'] = None
    scene['selection'] = 'auto'
    scene['star'] = None
    return project


def chosen(scene):
    """The take used downstream: the explicitly selected one, else the latest."""
    takes = scene.get('takes') or []
    ref, mode = selection_of(scene)
    if mode == 'explicit' and ref:
        for t in takes:
            if ref in (t.get('id'), t.get('run')):
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
        subprocess.run([FFMPEG, '-v', 'error', '-y', '-sseof', '-0.5', '-i', str(run / 'video.mp4'),
                        '-update', '1', str(out)], check=True)
        return out
    (frames, height, width, _), offset = enhance.npy_header(npy)
    size = height * width * 3
    with open(npy, 'rb') as f:
        f.seek(offset + (frames - 1) * size)
        raw = f.read(size)
    subprocess.run([FFMPEG, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
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


# ---------------------------------------------------------------- generation records
#
# Before a render or enhance job submits to ComfyUI, web.py writes an immutable request snapshot to
# projects/<slug>/jobs/<job_id>.json: the exact prompt, mode, settings, reference hashes, start frame,
# LoRAs and workflow graph handed over. That file is created once and never edited afterwards; results
# (take id, run, paths, prompt ids, timings) go to a sibling <job_id>.result.json, written once when
# the job ends -- the sibling file is what lets the snapshot stay untouched. refs-store/ holds one
# content-addressed copy of every input picture, so the sha256 in a snapshot can be turned back into
# the bytes it hashes. Takes made before this existed carry no `id`/`job`/`provenance`: take_provenance()
# reports them 'legacy' and nothing is ever back-filled.

JOBS_DIR = 'jobs'
REFS_STORE = 'refs-store'


def new_take_id():
    """Application-owned take id, stable across renames/moves; distinct from ComfyUI run names."""
    return secrets.token_hex(8)


def job_path(slug, job_id):
    if not re.fullmatch(r'[0-9a-f]{8,32}', job_id or ''):
        raise ProjectError('bad job id')
    return path(slug, JOBS_DIR, f'{job_id}.json')


def result_path(slug, job_id):
    return job_path(slug, job_id).with_name(f'{job_id}.result.json')


def _write_once(f, payload):
    """Create a record file atomically (tmp + replace); a generation record is never rewritten."""
    if f.exists():
        raise ProjectError(f'{f.name} already exists: generation records are immutable')
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f'{f.name}.tmp')
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(f)
    return f


def write_job_snapshot(slug, snapshot):
    return _write_once(job_path(slug, snapshot['id']), snapshot)


def write_job_result(slug, job_id, result):
    return _write_once(result_path(slug, job_id), result)


def read_job(slug, job_id):
    """(snapshot, result) for a job id; either None when its file is missing."""
    f = job_path(slug, job_id)
    snapshot = json.loads(f.read_text()) if f.is_file() else None
    r = result_path(slug, job_id)
    return snapshot, (json.loads(r.read_text()) if r.is_file() else None)


def store_ref(slug, name, raw):
    """Copy one input picture into the content-addressed refs-store (only when missing);
    return the snapshot entry: name in the job, hash, size, and the store path that can recreate it."""
    sha = hashlib.sha256(raw).hexdigest()
    ext = (Path(name).suffix or '.bin').lower()
    dest = path(slug, REFS_STORE, f'{sha}{ext}')
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    return {'name': name, 'sha256': sha, 'bytes': len(raw), 'store': f'{REFS_STORE}/{dest.name}'}


def take_provenance(take):
    """'recorded' for a take a job snapshot backs; 'legacy' for everything older, never invented."""
    return 'recorded' if take.get('provenance') == 'recorded' and take.get('id') else 'legacy'


def take_for_run(project, run):
    """(scene id, take) whose run matches `run`, or (None, None); enhances address takes by run."""
    for scene in project.get('scenes', []):
        for take in scene.get('takes') or []:
            if take.get('run') == run:
                return scene['id'], take
    return None, None


# ---------------------------------------------------------------- dependency status
#
# scene_status() answers, per scene and purely ON READ: does the take the scene currently resolves
# to still match what the scene would produce today? Every flag is derived from authoritative
# records only -- the selected take's job snapshot (jobs/<id>.json), the current scene fields, the
# reference pictures hashed now, the previous scene's current selection (explicit or auto), and the
# current bible/notes -- and NONE of it is ever stored: project.json gains no status key, so a
# flag can never go stale between a change and a save. File hashes replay from an in-memory cache
# keyed by path+mtime_ns+size, so a GET costs no rehashing while inputs are untouched.
#
# Flags speak about content validity, never about execution: running/failed/cancelled is job state
# and lives in the scene's `job`; a flagged take keeps its record and media, nothing is deleted or
# re-queued here. Flag vocabulary: state is 'same' (compared, matches), 'changed' (compared,
# differs), 'not_applicable' (the check does not apply to this scene/take) or 'unknown_legacy'
# (the check needs a snapshot the take does not have -- old takes answer this instead of guessing).
# `outdated` lists the flags that read 'changed' -- the scene is DIRECTLY outdated. `downstream_of`
# is separate and informational: scenes further along a continuation chain that a regeneration of
# this one would eventually affect; they are NOT outdated while their own immediate source stands.

STATUS_FLAGS = ('prompt_changed', 'refs_changed', 'settings_changed',
                'continuity_outdated', 'instructions_changed', 'finishing_changed')
HASH_CACHE = {}                                          # path -> (mtime_ns, size, sha256); memory only


def _file_sha256(f):
    """sha256 of a file, or None when it is gone; the bytes are re-read only when mtime_ns or size moved."""
    try:
        st = Path(f).stat()
    except OSError:
        return None
    key = str(f)
    hit = HASH_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    sha = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    if len(HASH_CACHE) > 512:
        HASH_CACHE.clear()
    HASH_CACHE[key] = (st.st_mtime_ns, st.st_size, sha)
    return sha


def instructions_of(project):
    """Hashes of the global text every director prompt is assembled from: the style bible and the
    director notes. New job snapshots carry these so editing the bible later can FLAG the prompts
    affected -- the scene text itself is never rewritten automatically."""
    return {'bible_sha256': hashlib.sha256((project.get('bible') or '').encode()).hexdigest(),
            'notes_sha256': hashlib.sha256((project.get('notes') or '').encode()).hexdigest()}


def _flag(state, reason, text):
    return {'state': state, 'reason': reason, 'text': text}


def _flag_compare(parts, what):
    """No parts -> 'same'; parts -> 'changed', the machine reason being the parts joined."""
    if parts:
        return _flag('changed', '; '.join(parts), f'{what} now differ from what produced the selected take: '
                                                  + '; '.join(parts) + '.')
    return _flag('same', '', f'{what} match the request that produced the selected take.')


def _loras_key(loras):
    return [(l['name'], float(l.get('strength', 1.0)), bool(l.get('enabled', True))) for l in loras or []]


def _downstream_scenes(project, i):
    """The continuation chain running forward from scene `i`: those scenes WOULD be affected if
    this scene's take were regenerated (their starting frame comes, hop by hop, from it). Each of
    them is judged only against its own immediate source, so a chain member is informational here,
    never marked outdated by an ancestor's status."""
    out = []
    for j in range(i + 1, len(project['scenes'])):
        s = project['scenes'][j]
        if s.get('connect') != 'continue':
            break
        via = project['scenes'][j - 1]
        out.append({'index': j, 'id': s['id'], 'title': s.get('title') or '', 'via': via['id'],
                    'reason': f"continues from scene {via['id']}",
                    'text': f'"{s.get("title") or s["id"]}" continues from "{via.get("title") or via["id"]}": '
                            'regenerating this scene would give it a new starting frame.'})
    return out

def _finishing_flag(project, take, snapshot):
    """One status flag: do the finishing settings now differ from what produced this take's
    finished version? The enhance job that made the final is the reference when the take
    names one (take['final_job']); a final-preset render recorded the finishing in its own
    snapshot. A finished version with no record anywhere answers unknown_legacy -- what an
    old run did is not invented here. Model filenames (upscaler, RIFE checkpoint) are
    config-level and deliberately not part of the comparison."""
    if not take.get('final'):
        return _flag('not_applicable', 'take has no finished version',
                     'The selected take has no finished version, so there is no finishing to compare.')
    record = None
    if take.get('final_job'):
        record = (read_job(project['slug'], take['final_job'])[0] or {}).get('finishing')
    elif snapshot is not None and snapshot.get('finishing'):
        record = snapshot['finishing']        # final render: render and finishing were one job
    used = finishing.settings_of_snapshot(record)
    if used is None:
        return _flag('unknown_legacy', 'no finishing record',
                     'The finished version of this take was made before job records named the '
                     'finishing settings (or its enhance job is gone): what it ran with cannot '
                     'be known without inventing it.')
    cur = finishing.for_project(project)
    parts = []
    if used['upscale'] != cur['upscale']:
        parts.append('upscaling ' + ('off' if not used['upscale']['enabled'] else f"{used['upscale']['height']}p")
                     + ' -> ' + ('off' if not cur['upscale']['enabled'] else f"{cur['upscale']['height']}p"))
    if used['interpolation'] != cur['interpolation']:
        parts.append('interpolation ' + ('off' if not used['interpolation']['enabled'] else f"x{used['interpolation']['factor']}")
                     + ' -> ' + ('off' if not cur['interpolation']['enabled'] else f"x{cur['interpolation']['factor']}"))
    if used['encode']['crf'] != cur['encode']['crf']:
        parts.append(f"crf {used['encode']['crf']} -> {cur['encode']['crf']}")
    return _flag_compare(parts, 'The finishing settings')


def scene_status(project, scene):
    """All derived dependency state for one scene, computed from records on disk plus the current
    project -- and stored nowhere. Keys: provenance ('recorded'/'legacy'/'none'), take (the
    selected take this verdict describes), flags (one entry per STATUS_FLAGS, each
    {'state', 'reason', 'text'}), outdated (flags reading 'changed' -- directly outdated),
    unknown_legacy (flags a legacy take cannot answer), downstream_of (informational chain)."""
    i = scene_index(project, scene['id'])
    take = chosen(scene)
    status = {'provenance': 'none', 'take': None, 'flags': {}, 'outdated': [], 'unknown_legacy': [],
              'downstream_of': _downstream_scenes(project, i)}
    flags = status['flags']
    if take is None:
        for name in STATUS_FLAGS:
            flags[name] = _flag('not_applicable', 'no take selected',
                                'The scene has no take to compare against yet.')
        return status

    provenance = take_provenance(take)
    snapshot = read_job(project['slug'], take['job'])[0] if provenance == 'recorded' and take.get('job') else None
    status['provenance'] = provenance
    status['take'] = {'id': take.get('id'), 'ref': take_ref(take), 'job': take.get('job'), 'run': take['run']}
    missing = _flag('unknown_legacy', 'no generation snapshot',
                    'The selected take predates generation records (or its snapshot is gone): '
                    'this cannot be checked without inventing what the old run did.')

    f = {}

    if snapshot is None:
        f['prompt_changed'] = f['refs_changed'] = f['settings_changed'] = f['instructions_changed'] = missing
    else:
        parts = []
        want = ensure_i2va(scene.get('prompt') or '') if snapshot.get('first') else (scene.get('prompt') or '')
        if want != (snapshot.get('prompt') or ''):
            parts.append('prompt text')
        # a continue scene's submitted mode is forced by the starting frame, so its prompt_mode
        # (written before the frame existed) is not comparable to the snapshot's mode.
        if scene.get('prompt_mode') and scene['connect'] != 'continue' \
                and scene['prompt_mode'] != snapshot.get('mode'):
            parts.append(f"mode {snapshot.get('mode')} -> {scene['prompt_mode']}")
        f['prompt_changed'] = _flag_compare(parts, 'The prompt and mode')

        snap_refs = snapshot.get('refs') or []
        parts = []
        for r in snap_refs:
            sha = _file_sha256(path(project['slug'], 'refs', r['name']))
            if sha is None:
                parts.append(f"{r['name']} missing")
            elif sha != r['sha256']:
                parts.append(f"{r['name']} content")
        if not parts and not snapshot.get('first') and len(snap_refs) != len(project.get('refs') or []):
            parts.append(f"count {len(snap_refs)} -> {len(project.get('refs') or [])}")
        f['refs_changed'] = _flag_compare(parts, 'The reference pictures')

        parts = []
        if snapshot.get('seed') != scene.get('seed'):
            parts.append(f"seed {snapshot.get('seed')} -> {scene.get('seed')}")
        size = project.get('size') or []
        if [snapshot.get('width'), snapshot.get('height')] != list(size):
            parts.append(f"size {snapshot.get('width')}x{snapshot.get('height')} -> {'x'.join(map(str, size))}")
        if snapshot.get('seconds') != scene.get('seconds'):
            parts.append(f"seconds {snapshot.get('seconds')} -> {scene.get('seconds')}")
        if _loras_key(snapshot.get('loras')) != _loras_key(project.get('loras')):
            parts.append('loras')
        f['settings_changed'] = _flag_compare(parts, 'The generation settings')

        snap_instr = snapshot.get('instructions')
        if not snap_instr:
            f['instructions_changed'] = _flag('unknown_legacy', 'no instructions hash in snapshot',
                                              'This take was recorded before the style bible/notes were hashed: '
                                              'whether the bible changed since cannot be known.')
        else:
            now = instructions_of(project)
            parts = [label for k, label in (('bible_sha256', 'bible'), ('notes_sha256', 'notes'))
                     if now[k] != snap_instr.get(k)]
            f['instructions_changed'] = _flag_compare(parts, 'The global style bible/notes')
            f['instructions_changed']['text'] += ' Prompts are flagged only, never rewritten.'

    if scene.get('connect') != 'continue' or not i:
        f['continuity_outdated'] = _flag('not_applicable', 'not a continuation',
                                         'The scene does not start from the previous scene, so no upstream take matters.')
    else:
        prev = project['scenes'][i - 1]
        if snapshot is not None:
            first = snapshot.get('first')
            src = dict(first['source'], frame_sha256=first.get('sha256')) if first and first.get('source') else False
        else:
            src = take.get('source_take')                     # legacy: only data the record itself holds
        if src is False:
            f['continuity_outdated'] = _flag('not_applicable', 'take had no starting frame',
                                             'The selected take was generated without a frame from the previous scene.')
        elif not src:
            f['continuity_outdated'] = _flag('unknown_legacy', 'no recorded source take',
                                             'The legacy take records no upstream take, so its continuity cannot be '
                                             'checked without inventing what it started from.')
        else:
            cur = chosen(prev)
            parts = []
            if not cur:
                parts.append('previous scene has no take')
            elif src.get('scene') != prev['id']:              # scenes were reordered since the take ran
                parts.append(f"previous scene {src.get('scene')} -> {prev['id']}")
            elif src.get('take') != take_ref(cur) or src.get('run') != cur['run']:
                parts.append(f"source take {src.get('take')} -> {take_ref(cur)}")
            f['continuity_outdated'] = _flag_compare(parts, 'The upstream take this continuation started from')

    f['finishing_changed'] = _finishing_flag(project, take, snapshot)

    for name in STATUS_FLAGS:
        flags[name] = f[name]
        if f[name]['state'] == 'changed':
            status['outdated'].append(name)
        elif f[name]['state'] == 'unknown_legacy':
            status['unknown_legacy'].append(name)
    return status


# ---------------------------------------------------------------- cut fingerprint
# Every assembled cut records a fingerprint: sha256 over a canonical JSON plan of the exact
# inputs -- ordered takes (with each take's final/draft choice), per-scene transitions, the
# title/end cards, the music bed, and the export settings assemble() used. On read an output is
# re-planned from the CURRENT project (no ffmpeg, stored nowhere): components that would cut
# differently come back named in `changes`. Cuts assembled before fingerprints existed answer
# 'unknown_legacy' instead of guessing, and old entries and files are never edited.

CUT_COMPONENTS = ('takes', 'transitions', 'cards', 'music', 'export')
MUSIC_VOLUME = '0.6'                              # keep in step with the ffmpeg flags below
LOUDNORM = 'I=-14:TP=-1.5:LRA=11'


def _card_plan(card):
    return None if not card else {'text': card['text'], 'seconds': float(card['seconds'])}


def cut_plan(project, used_takes, kind='film'):
    """The canonical input plan of one cut, as a plain JSON-able dict. Pure: reads the project
    dict and the frozen used_takes list and touches nothing else. `kind` selects the same
    size/fps/encoding assemble() uses; each take's final/draft choice is the one a cut of THIS
    project state would make (film prefers a take's enhanced final when it has one)."""
    w, h = FILM_SIZES[project['aspect']] if kind == 'film' else project['size']
    takes = []
    for u in used_takes:
        scene = next((s for s in project['scenes'] if s['id'] == u['scene']), None)
        take = find_take(scene, u['take']) if scene else None
        takes.append({'scene': u['scene'], 'take': u['take'], 'run': u['run'],
                      'selection': u['selection'],
                      'file': 'final' if kind == 'film' and take and take.get('final') else 'draft'})
    music = None
    name = project.get('music_file')
    if name and path(project['slug'], name).is_file():
        music = {'file': name, 'hash': hashlib.sha256(path(project['slug'], name).read_bytes()).hexdigest(),
                 'brief_hash': hashlib.sha256((project.get('music_brief') or '').encode()).hexdigest(),
                 'volume': MUSIC_VOLUME, 'loudnorm': LOUDNORM}
    st = finishing.for_project(project)          # one fps policy: the film's fps and crf come
    fps = finishing.output_fps(finishing.render_fps(), st) \
        if kind == 'film' else PREVIEW_FPS       # from the finishing settings, not a second
    crf = str(st['encode']['crf']) if kind == 'film' else str(PREVIEW_CRF)   # draft constant
    export = {'width': int(w), 'height': int(h), 'fps': fps,
              'video_codec': 'libx264', 'preset': 'medium', 'crf': crf,
              'pix_fmt': 'yuv420p', 'audio_codec': 'aac', 'audio_bitrate': '192k',
              'audio_rate': 48000, 'faststart': True, 'crossfade': XFADE, 'dip': DIP}
    export.update(finishing.plan_extras(st))     # stage switches join the plan as soon as the
    return {'takes': takes,                      # project leaves the legacy pipeline
            'transitions': [s.get('transition', 'cut') for s in project['scenes']],
            'cards': {'title': _card_plan(project.get('title_card')), 'end': _card_plan(project.get('end_card'))},
            'music': music,
            # the encoder flags assemble() puts on its command line; change both together
            'export': export}


def _plan_fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cut_fingerprint(project, used_takes, kind='film'):
    return _plan_fingerprint(cut_plan(project, used_takes, kind))


def current_used_takes(project, kind='film'):
    """The used_takes a fresh assemble(kind) of THIS project state would record: each scene's
    resolved take in scene order. Resolution mirrors chosen()/selection_of(); ffmpeg never runs."""
    used = []
    for scene in project['scenes']:
        take = chosen(scene)
        if not take:
            continue
        used.append({'scene': scene['id'], 'take': take_ref(take), 'run': take['run'],
                     'selection': selection_of(scene)[1]})
    return used


def current_cut_fingerprint(project, kind='film'):
    return cut_fingerprint(project, current_used_takes(project, kind), kind)


def output_status(project, kind, entry):
    """Derived per-output state for the read view, stored nowhere: 'fingerprint' -- the one the
    entry was cut with, or 'unknown_legacy' for cuts predating fingerprints; 'outdated'; and
    'changes', the components a fresh assemble(kind) would cut differently. A fingerprint with
    no stored plan cannot be compared: it reports changes nothing rather than guessing."""
    if not entry.get('fingerprint') or not entry.get('cut_plan'):
        return {'fingerprint': entry.get('fingerprint') or 'unknown_legacy', 'outdated': False, 'changes': []}
    current = cut_plan(project, current_used_takes(project, kind), kind)
    changes = [c for c in CUT_COMPONENTS if entry['cut_plan'].get(c) != current.get(c)]
    return {'fingerprint': entry['fingerprint'], 'outdated': bool(changes), 'changes': changes}


# ---------------------------------------------------------------- assembly

def _duration(file):
    out = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(file)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _has_audio(file):
    out = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
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
    cmd = [FFMPEG, '-v', 'error', '-y',
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


def assemble(project, kind, cancel=None):
    """Cut each scene's resolved take into one video, recording per scene which take -- id and
    whether the pick was explicit or automatic -- actually went into the cut, so an export never
    silently describes something else afterwards.
    kind: 'animatic' (draft takes, the preview fps) or 'film' (the finished finals at the
    fps the project's finishing policy says -- one fps decision, finishing.output_fps()).
    cancel(): turn true and the running encode is killed and the cut abandoned with
    ProjectError('cancelled'). Every clip -- whatever rate it was made at -- passes through
    the same fps filter, so mixed-rate takes cut into one film without a second policy."""
    clips, missing, not_final, used_takes = [], [], [], []
    for i, scene in enumerate(project['scenes'], 1):
        take = chosen(scene)
        if not take:
            missing.append(f'{i} {scene["title"]}')
            continue
        used_takes.append({'scene': scene['id'], 'take': take_ref(take), 'run': take['run'],
                           'selection': selection_of(scene)[1]})
        source = take.get('final') if kind == 'film' and take.get('final') else take['path']
        if kind == 'film' and not take.get('final'):
            not_final.append(i)
        clips.append(Path(source))
    if missing:
        raise ProjectError('scenes without a take: ' + ', '.join(missing))
    if not clips:
        raise ProjectError('no scenes')
    w, h = FILM_SIZES[project['aspect']] if kind == 'film' else project['size']
    plan = cut_plan(project, used_takes, kind)      # inputs frozen before ffmpeg runs
    fps = finishing.output_fps(finishing.render_fps(), finishing.for_project(project)) \
        if kind == 'film' else PREVIEW_FPS          # the one fps policy decides the film rate
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
    cmd, parts, dur = [FFMPEG, '-v', 'error', '-y'], [], []
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
        parts += [f'[{m}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{tcur:.3f},volume={MUSIC_VOLUME},'
                  f'afade=t=in:d=0.5,afade=t=out:st={tcur - fade:.3f}:d={fade:.3f}[mus]',
                  f'[{acur}]asplit=2[dia1][key]',
                  '[mus][key]sidechaincompress=threshold=0.04:ratio=6:attack=20:release=500[duck]',
                  '[dia1][duck]amix=inputs=2:duration=first:normalize=0[mix]',
                  f'[mix]loudnorm={LOUDNORM}[aout]']
    else:
        parts.append(f'[{acur}]loudnorm={LOUDNORM}[aout]')
    out = path(project['slug'], f'{kind}-{time.strftime("%Y%m%d-%H%M%S")}.mp4')
    cmd += ['-filter_complex', ';'.join(parts), '-map', f'[{vcur}]', '-map', '[aout]',
            '-c:v', 'libx264', '-preset', 'medium',
            '-crf', str(finishing.for_project(project)['encode']['crf']) if kind == 'film' else str(PREVIEW_CRF),
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', str(out)]
    if cancel and cancel():
        if cards:
            cards.cleanup()
        raise ProjectError('cancelled')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while proc.poll() is None:
        if cancel and cancel():
            proc.kill()
            proc.communicate()
            if cards:
                cards.cleanup()
            raise ProjectError('cancelled')
        time.sleep(0.15)
    _stdout, err = proc.communicate()
    if cards:
        cards.cleanup()
    if proc.returncode:
        raise ProjectError('ffmpeg failed: ' + err.strip()[-600:])
    return {'file': out.name, 'seconds': round(tcur, 1), 'scenes': n_scenes, 'not_final': not_final,
            'used_takes': used_takes, 'created': time.time(),
            'fingerprint': _plan_fingerprint(plan), 'cut_plan': plan}
