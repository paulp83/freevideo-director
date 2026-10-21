"""Build a throwaway six-scene film with real media for the browser layout checks.

`build(root)` makes everything inside `root` (a temp dir the caller owns), never touching real
projects: two reference pictures (real PNGs), a six-scene storyboard, real ffmpeg-generated clips
(16:9 testsrc2 with a distinct hue and caption per take) laid out like FreeVideo run dirs
(`video.mp4` + `video.artifacts/`), recorded generation snapshots + take records exactly as
web.py's render_job would write them, per-take JPG thumbnails (`thumb_url`, the not-yet-produced
server field the page already reads), an explicit take pick on scene 1, a continuation scene
whose take came from scene 1's chosen take's real last frame, one enhanced (final) take, one
legacy pre-records take, one scene whose prompt was edited after its take (stale on purpose),
and both assembled cuts (animatic + film) via the real `projects.assemble`.

Run standalone to inspect the result:  python3 tests/fixtures/make_fixture.py --root DIR [--keep]
Prints a JSON manifest (slug, scene ids, take ids) on stdout.
"""
import argparse, json, secrets, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent          # repo root
sys.path.insert(0, str(HERE))
import enhance                                                 # noqa: E402
import project as projects                                     # noqa: E402

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

# title, seconds, connect, action, hue, tone Hz
PLAN = [
    ('Wide meadow',   5, 'cut',      'The hare crosses the meadow at dawn.',            0, 220),
    ('Running on',    4, 'continue', 'The hare keeps running towards the gate.',        60, 330),
    ('The gate',      6, 'cut',      'The hare squeezes through the open gate.',       120, 440),
    ('Lantern',       5, 'cut',      'A lantern swings on the far side of the fence.', 180, 550),
    ('The fence',     4, 'cut',      'The hare vaults the low fence.',                 240, 660),
    ('Dusk',          6, 'cut',      'The meadow goes quiet at dusk.',                 300, 770),
]


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clip(dst, seconds, hue, freq, label):
    """A short 16:9 clip: testsrc2 shifted to a distinct hue, a tone, and a caption."""
    _run(['ffmpeg', '-v', 'error', '-y',
          '-f', 'lavfi', '-i', f'testsrc2=size=320x180:rate=24:duration={seconds}',
          '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={seconds}',
          '-vf', f'hue=h={hue},drawtext=fontfile={FONT}:text={label!r}:x=(w-tw)/2:y=h-th-16'
                 f':fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6',
          '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30', '-pix_fmt', 'yuv420p',
          '-c:a', 'aac', '-shortest', str(dst)])


def png(dst, src):
    _run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', src, '-frames:v', '1', str(dst)])


def thumb(video, dst):
    _run(['ffmpeg', '-v', 'error', '-y', '-ss', '1', '-i', str(video), '-frames:v', '1',
          '-vf', 'scale=160:-1', '-q:v', '6', str(dst)])


def _png_bytes(src):
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'ref.png'
        png(out, src)
        return out.read_bytes()


def build(root):
    """Create the fixture film under `root`; return a manifest describing it."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    projects.ROOT = root / 'projects'
    comfy = root / 'comfy-output'
    slug = projects.create('Meadow chase', 'A hare crosses the meadows at dawn.', 30, '16:9',
                           [('meadow.png', _png_bytes('mandelbrot=size=256x144')),
                            ('hare.png', _png_bytes('testsrc=size=256x144:duration=1'))])['slug']

    def tune(p):
        p['bible'] = 'A quiet nature chase. The hare is quick but never panicked; dawn light throughout.'
        p['notes'] = 'Keep the meadow wide. No dialogue. The gate squeaks.'
        p['music_brief'] = 'Light strings, playful, quieting at dusk.'
        p['loras'] = projects.clean_loras([{'name': 'meadow-style.safetensors', 'strength': 0.8}])
    projects.mutate(slug, tune)

    bible = projects.load(slug)['bible']
    plan = {'title': 'Meadow chase', 'bible': bible, 'music': 'Light strings, playful, quieting at dusk.',
            'scenes': [{'title': t, 'seconds': s, 'connect': c, 'action': a} for t, s, c, a, _h, _f in PLAN]}
    projects.mutate(slug, lambda p: projects.apply_storyboard(p, plan))
    prompts = {t: f'{a} Wide shot, dawn light.' for t, _s, _c, a, _h, _f in PLAN}
    projects.mutate(slug, lambda p: [s.__setitem__('prompt', prompts[s['title']]) for s in p['scenes']])
    ids = [s['id'] for s in projects.load(slug)['scenes']]
    manifest = {'slug': slug, 'scene_ids': ids, 'takes': {}}

    def take(i, label, prompt, seed, mode='ref2va', first=None, source=None, final=False):
        """One take: real clip laid out like a FreeVideo run, a snapshot, a take record, a thumb."""
        tid, jid = projects.new_take_id(), secrets.token_hex(8)
        title, seconds, _c, _a, hue, freq = PLAN[i]
        run = comfy / f'run-{i}-{tid}'
        (run / 'video.artifacts').mkdir(parents=True, exist_ok=True)
        clip(run / 'video.mp4', seconds, hue, freq, f'{title} {label}')
        media = projects.path(slug, 'media', f'{tid}.mp4'); media.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run / 'video.mp4', media)
        thumbs = projects.path(slug, 'thumbs'); thumbs.mkdir(exist_ok=True)
        thumb(run / 'video.mp4', thumbs / f'{tid}.jpg')
        p = projects.load(slug)
        scene = p['scenes'][i]
        refs = [dict(projects.store_ref(slug, n, projects.path(slug, 'refs', n).read_bytes()), index=n)
                for n in ([] if first else p['refs'])]
        firsts = dict(projects.store_ref(slug, 'frame.png', first), source=source) if first else None
        projects.write_job_snapshot(slug, {
            'id': jid, 'kind': 'render-draft', 'created': time.time(), 'project': slug,
            'scene': str(scene['id']), 'take_id': tid, 'preset': 'draft', 'variant': None,
            'mode': mode, 'prompt': projects.ensure_i2va(prompt) if first else prompt,
            'seed': seed, 'width': p['size'][0], 'height': p['size'][1], 'seconds': scene['seconds'],
            'two_pass': mode == 'ref2va', 'refs': refs, 'first': firsts, 'loras': p['loras'], 'graph': {},
            'director': {'model': None, 'skill_revision': None},
            'instructions': projects.instructions_of(p), 'finishing': None})
        src = dict(source, frame_sha256=firsts['sha256']) if firsts else None
        rec = {'id': tid, 'job': jid, 'provenance': 'recorded', 'run': str(run), 'path': str(run / 'video.mp4'),
               'url': f'/files/{slug}/media/{tid}.mp4', 'preset': 'draft', 'seed': seed, 'created': time.time(),
               'final': None, 'final_url': None, 'variant': None, 'source_take': src,
               'thumb_url': f'/files/{slug}/thumbs/{tid}.jpg'}
        if final:                                   # an enhanced take: a real "finished" clip next to the run
            clip(run / 'final.mp4', seconds, hue + 30, freq, f'{title} {label} final')
            fmed = projects.path(slug, 'media', f'{tid}-final.mp4')
            shutil.copyfile(run / 'final.mp4', fmed)
            rec['final'] = str(run / 'final.mp4'); rec['final_url'] = f'/files/{slug}/media/{tid}-final.mp4'
            projects.write_job_snapshot(slug, {
                'id': secrets.token_hex(8), 'kind': 'enhance', 'created': time.time(), 'project': slug,
                'scene': str(scene['id']),
                'source': {'take': tid, 'run': str(run), 'provenance': 'recorded'},
                'finishing': dict(enhance.DEFAULTS, fps=enhance.FPS), 'instructions': projects.instructions_of(p)})
        projects.mutate(slug, lambda p, i=i, rec=rec, seed=seed: (
            p['scenes'][i]['takes'].append(rec), p['scenes'][i].__setitem__('seed', seed)))
        manifest['takes'].setdefault(i, []).append(tid)
        return rec

    # scene 1: two takes, the first explicitly pinned -> explicit selection state
    t0a = take(0, 'take 1', prompts['Wide meadow'], 11)
    take(0, 'take 2', prompts['Wide meadow'], 12)
    projects.mutate(slug, lambda p: projects.set_explicit(p, ids[0], t0a['id']))

    # scene 2 (continue): its take came from scene 1's chosen take's real last frame
    src1 = projects.chosen(projects.load(slug)['scenes'][0])
    frame = projects.last_frame(projects.load(slug), src1['run']).read_bytes()
    take(1, 'take 1', prompts['Running on'], 21, mode='i2va', first=frame,
         source={'scene': ids[0], 'take': src1['id'], 'run': src1['run']})

    # scene 3: three takes, latest auto-chosen; the newest one carries a real enhanced final
    take(2, 'take 1', prompts['The gate'], 31)
    take(2, 'take 2', prompts['The gate'], 32)
    take(2, 'take 3', prompts['The gate'], 33, final=True)

    take(3, 'take 1', prompts['Lantern'], 41)

    # scene 5: a legacy take from before generation records -- no id/job/provenance, but real media
    title, seconds, _c, _a, hue, freq = PLAN[4]
    lrun = comfy / f'run-legacy-{ids[4][:8]}'
    (lrun / 'video.artifacts').mkdir(parents=True, exist_ok=True)
    clip(lrun / 'video.mp4', seconds, hue, freq, f'{title} legacy')
    ltid = projects.new_take_id()
    lmedia = projects.path(slug, 'media', f'{ltid}.mp4'); lmedia.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(lrun / 'video.mp4', lmedia)
    projects.path(slug, 'thumbs').mkdir(exist_ok=True)
    thumb(lrun / 'video.mp4', projects.path(slug, 'thumbs', f'{ltid}.jpg'))
    projects.mutate(slug, lambda p: p['scenes'][4]['takes'].append(
        {'run': str(lrun), 'path': str(lrun / 'video.mp4'), 'url': f'/files/{slug}/media/{ltid}.mp4',
         'preset': 'draft', 'seed': 7, 'created': time.time(), 'final': None, 'final_url': None,
         'variant': None, 'source_take': None, 'thumb_url': f'/files/{slug}/thumbs/{ltid}.jpg'}))
    manifest['takes'].setdefault(4, []).append(ltid)

    # scene 6: take first, then the prompt was edited -> prompt_changed
    take(5, 'take 1', prompts['Dusk'], 61)
    projects.mutate(slug, lambda p: p['scenes'][5].__setitem__(
        'prompt', 'The meadow goes quiet at dusk; the hare sits very still.'))

    # real assembled cuts from the real clips
    def assemble(p, kind):
        p['outputs'][kind] = projects.assemble(p, kind)
    projects.mutate(slug, lambda p: assemble(p, 'animatic'))
    projects.mutate(slug, lambda p: assemble(p, 'film'))
    return manifest


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--keep', action='store_true', help='leave the fixture dir in place')
    a = ap.parse_args()
    m = build(a.root)
    print(json.dumps(m, indent=2))
    if not a.keep:
        shutil.rmtree(a.root, ignore_errors=True)
        print(f'(removed {a.root}; pass --keep to inspect)')
