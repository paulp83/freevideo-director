#!/usr/bin/env python3
"""M6 pilot: a deliberately scoped A/B video comparison, 3 scenes x 2 seeds x 2 arms = 12 slots.

Arm 'simple' renders the user's plain idea text as the prompt; arm 'director' renders the
prompt director.py writes for the same idea and reference pictures. Everything about one pilot
run lives under pilot-runs/<id>/ (gitignored): the immutable manifest written by `plan`, the
blind-review clips and page written by `review-prepare`, the key kept OUTSIDE review/, the
append-only ratings written by the review server, and report.md/report.json from `report`.

  python3 pilot.py plan    --id spring                    # writes manifest.json (immutable)
  python3 pilot.py prompts --id spring                    # arm B via director.py, arm A = idea text
  python3 pilot.py render  --id spring                    # 12 slots ONE AT A TIME via web.py's
                                                          # tracked submit path (finish --help)
  python3 pilot.py review-prepare --id spring             # blinded clip-NN.mp4 + review/index.html
  python3 pilot.py review-serve  --id spring              # 127.0.0.1 review page -> ratings.json
  python3 pilot.py report --id spring                     # refuses until every clip is rated

docs/pilot.md carries the protocol, the scene choices, the blinding design and its limits.
Stdlib only. Render goes through web.py's submit_tracked/jobs_db path (client-supplied prompt
ids, nothing auto-resubmitted), so a crash is recoverable exactly like a UI render.

The one thing this tool must never claim: prompt length, keyword matches or format compliance
are NOT evidence of superior video quality; the report only accepts, per-criterion scores and
time - measured two ways, backend execution time kept apart from wall time.
"""
import argparse, hashlib, json, os, random, re, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config, director, eval as evalh, h3check, readiness, web

HERE = Path(__file__).resolve().parent
PILOT_ROOT = HERE / 'pilot-runs'          # module global: tests point it at a temp dir
SCENES_DEFAULT = HERE / 'pilot' / 'scenes.json'
ARMS = ('simple', 'director')
CRITERIA = ('action', 'identity', 'motion', 'cinematography', 'audio')
CRITERION_LABEL = {'action': 'Action adherence', 'identity': 'Identity', 'motion': 'Motion',
                   'cinematography': 'Cinematography', 'audio': 'Audio'}
SCHEMA_VERSION = 1
CAUTION = ('Prompt length, keyword matches and format compliance are NOT evidence of superior '
           'video quality. This report therefore shows none of them: only acceptance, the five '
           'blinded per-criterion scores, and time measured two ways (backend execution time '
           'kept separate from wall time).')
_SMALL_N = ('n = 6 clips per arm at most: far too small for any statistical conclusion. The '
            'tables below are the raw data with counts and ranges shown - treat differences as '
            'directional, not proven.')

# fixture env names (from the scenes file) mapped to config.py settings; never read raw from env
ENV_SETTING = {'PILOT_PORTRAIT_IMAGE': 'pilot_portrait_image'}


class PilotError(Exception):
    """A refused pilot operation; the message says exactly why."""


def _utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def run_dir(pilot_id):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', pilot_id or ''):
        raise PilotError('pilot id must match [a-z0-9][a-z0-9-]{0,63}')
    return PILOT_ROOT / pilot_id


def manifest_path(pilot_id):
    return run_dir(pilot_id) / 'manifest.json'


def load_manifest(pilot_id):
    path = manifest_path(pilot_id)
    if not path.is_file():
        raise PilotError(f'no manifest at {path}; run `pilot.py plan --id {pilot_id}` first')
    return json.loads(path.read_text())


def save_manifest(manifest):
    """The manifest is immutable after plan EXCEPT for the per-slot result fields; enforce that
    here: a rewrite may only change slot entries. Atomic, so a crash keeps the last good copy."""
    path = manifest_path(manifest['id'])
    old = json.loads(path.read_text())
    if any(manifest[k] != old[k] for k in old if k != 'slots'):
        raise PilotError('manifest is immutable after plan except for slot result fields')
    tmp = path.with_name('.manifest.tmp')
    tmp.write_text(json.dumps(manifest, indent=1) + '\n')
    os.replace(tmp, path)


# ---------------------------------------------------------------- plan

def _sha(path, width=16):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:width]


def _comfy_build():
    """The ComfyUI build string, best effort (plan runs offline and records that it could not ask)."""
    try:
        stats = web.comfy('/system_stats')
        system = stats.get('system', {}) if isinstance(stats, dict) else {}
        return {'comfyui_version': system.get('comfyui_version'), 'system': {
            k: system.get(k) for k in ('os', 'python_version') if k in system}}
    except Exception as e:                       # offline / unreachable: recorded, plan still works
        return {'error': f'{type(e).__name__}: {e}'}


def _director_probe(url, model):
    """Which director model a `prompts` run would use + the ids /v1/models reports; best effort."""
    if not url:
        try:
            url, model = director.backend()
        except director.DirectorError as e:
            return {'error': str(e)}
    return {'url': url, 'configured_model': model or None, 'models_endpoint': evalh.models_at(url)}


def load_scenes(path):
    data = json.loads(Path(path).read_text())
    scenes = data.get('scenes') or []
    if len(scenes) != 3:
        raise PilotError(f'the pilot design is exactly three scenes ({len(scenes)} in {path})')
    if len({s['id'] for s in scenes}) != 3:
        raise PilotError(f'duplicate scene ids in {path}')
    for s in scenes:
        if not 4 <= s['seconds'] <= 15:
            raise PilotError(f"scene {s['id']}: seconds must be 4-15 (H3 range)")
    return data


def resolve_fixtures(data, fixture_dir):
    """Resolve every fixture id of the scenes file to an existing path (generated ones built on
    demand; private ones by the eval.py lookup order: config override, fixture dir, repo default).
    Returns {fid: {kind, path or None, tried}}."""
    fixtures, out = data['fixtures'], {}
    needed = sorted({f for s in data['scenes'] for f in s.get('refs', [])})
    err = evalh.prepare_generated(fixtures, fixture_dir, needed, config.value('ffmpeg'))
    if err:
        raise PilotError(f'cannot build generated fixtures: {err}')
    overrides = {env: str(config.value(setting)) for env, setting in ENV_SETTING.items()
                 if config.value(setting)}
    for fid, spec in fixtures.items():
        tried = evalh.fixture_search(fid, fixtures, fixture_dir, overrides)
        found = next((p for p in tried if Path(p).is_file()), None)
        out[fid] = {'kind': spec['kind'], 'path': str(found) if found else None,
                    'tried': [str(p) for p in tried]}
    return out


def plan(args):
    if args.id is None:
        raise PilotError('--id is required')
    root = run_dir(args.id)
    if manifest_path(args.id).is_file():
        raise PilotError(f'{manifest_path(args.id)} exists; the manifest is immutable, '
                         f'refuse to overwrite (use a new --id to re-plan)')
    data = load_scenes(args.scenes)
    seeds = [int(x) for x in re.split(r'[,\s]+', args.seeds.strip()) if x != '']
    if len(seeds) != 2 or len(set(seeds)) != 2:
        raise PilotError(f'--seeds must name exactly two distinct seeds, got {seeds}')
    resolved = resolve_fixtures(data, args.fixture_dir)
    for scene in data['scenes']:
        for fid in scene.get('refs', []):
            if resolved[fid]['path'] is None:
                raise PilotError(f"fixture {fid} for scene {scene['id']} not found; tried: "
                                 + ', '.join(resolved[fid]['tried']))
        pv = scene.get('private_variant')
        if pv and resolved[pv]['path']:
            scene['resolved_refs'] = scene['refs'] + [pv]
            print(f"plan: scene {scene['id']} picks up the private variant {pv} "
                  f"({resolved[pv]['path']}) as an extra reference for both arms")
        elif pv:
            print(f"plan: private variant {pv} for scene {scene['id']} not supplied "
                  f"(tried: {', '.join(resolved[pv]['tried'])}); the scene stays portable")
        scene.setdefault('resolved_refs', list(scene.get('refs', [])))

    slots, order = {}, []
    for scene in data['scenes']:
        for seed in seeds:
            for arm in ARMS:
                slot = f"{scene['id']}-s{seed}-{arm}"
                slots[slot] = {'slot': slot, 'scene': scene['id'], 'arm': arm, 'seed': seed,
                               'prompt': None, 'render': None}
                order.append(slot)
    order_seed = args.order_seed if args.order_seed is not None else random.randrange(1 << 31)
    random.Random(order_seed).shuffle(order)
    for i, slot in enumerate(order):
        slots[slot]['order'] = i

    manifest = {
        'kind': 'freevideo-pilot', 'schema_version': SCHEMA_VERSION, 'id': args.id,
        'created': _utc(),
        'seeds': seeds,
        'order_seed': order_seed, 'order': order,
        'settings': {
            'width': args.width, 'height': args.height, 'two_pass': args.two_pass,
            'seconds_per_scene': {s['id']: s['seconds'] for s in data['scenes']},
            'finishing': 'off (the pilot compares raw generations: no enhance stage, '
                         'so no SeedVR2/RIFE ever touches the clips)',
        },
        'scenes': data['scenes'], 'fixtures': resolved,
        'versions': {
            'python': sys.version.split()[0], 'git': evalh.git_state(),
            'skill_revision': director.skill_revision(),
            'h3check_version': h3check.H3CHECK_VERSION, 'h3check_sha256': _sha(HERE / 'h3check.py'),
            'scenes_sha256': _sha(args.scenes),
            'director': _director_probe(config.value('director_url') or None,
                                        config.value('director_model') or None),
            'comfyui': _comfy_build(),
        },
        'slots': slots,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n')
    print(f'plan: {len(order)} slots (3 scenes x 2 seeds x 2 arms) in {manifest_path(args.id)}')
    print('plan: render order is randomised (order_seed recorded) so time-of-day/thermal '
          'effects cannot line up with an arm')
    return 0


# ---------------------------------------------------------------- prompts

def _idea_of(manifest, scene_id):
    return next(s for s in manifest['scenes'] if s['id'] == scene_id)['idea']


def _refs_of(manifest, scene_id):
    scene = next(s for s in manifest['scenes'] if s['id'] == scene_id)
    return [manifest['fixtures'][fid]['path'] for fid in scene['resolved_refs']]


def cmd_prompts(args):
    manifest = load_manifest(args.id)
    url = config.value('director_url') or None
    model = config.value('director_model') or None
    failed = 0
    for slot_id in manifest['order']:
        slot = manifest['slots'][slot_id]
        if slot['prompt'] and not (args.redo_slot and slot_id == args.redo_slot):
            print(f'prompts: {slot_id}: kept (already recorded; --redo-slot to rewrite)')
            continue
        idea = _idea_of(manifest, slot['scene'])
        if slot['arm'] == 'simple':
            # Arm A: the plain idea text is the whole prompt. The only wrapper the video model
            # needs is graph-level (seconds/size/seed), identical for both arms - docs/pilot.md.
            slot['prompt'] = {'arm': 'simple', 'text': idea, 'wrapper': 'none',
                              'chars': len(idea), 'at': _utc()}
        else:
            refs = _refs_of(manifest, slot['scene'])
            seconds = manifest['settings']['seconds_per_scene'][slot['scene']]
            start = time.time()
            try:
                rep = director.direct_report(idea, seconds=seconds, refs=refs, url=url, model=model)
                slot['prompt'] = {'arm': 'director', 'text': rep['text'], 'mode': rep['mode'],
                                  'model': rep['model'], 'calls': rep['calls'],
                                  'first_failures': rep['first_failures'],
                                  'final_failures': rep['final_failures'], 'retried': rep['retried'],
                                  'chars': len(rep['text']),
                                  'seconds_spent': round(time.time() - start, 1), 'at': _utc()}
                bad = rep['final_failures']
                print(f"prompts: {slot_id}: {rep['model']} in {rep['calls']} call(s); "
                      f"format checks: {'all pass' if not bad else 'still failing ' + ', '.join(bad)}"
                      ' (check results are validation bookkeeping, not a quality score)')
            except director.DirectorError as e:      # a failed call is a recorded row, never dropped
                failed += 1
                slot['prompt'] = {'arm': 'director', 'error': str(e),
                                  'error_class': type(e).__name__,
                                  'calls': getattr(e, 'calls', 0),
                                  'seconds_spent': round(time.time() - start, 1), 'at': _utc()}
                print(f'prompts: {slot_id}: FAILED ({type(e).__name__}: {e}); recorded, '
                      'nothing resubmitted')
        save_manifest(manifest)                      # crash-safe: every slot lands as it is made
    print('prompts: lengths are descriptive only - prompt length is NOT a quality measure:')
    for slot_id in manifest['order']:
        p = manifest['slots'][slot_id]['prompt']
        if p:
            print(f'  {slot_id}: {p.get("chars", "?")} chars'
                  + ('' if 'text' in p else '  (no prompt: the call failed)'))
    return 1 if failed else 0


# ---------------------------------------------------------------- render

def history_times(pid):
    """(execution_start_ms, execution_success_ms) epoch millis from ComfyUI /history
    status.messages, either None when the installed ComfyUI does not stamp them (the docs
    checklist asks the user to verify they exist)."""
    try:
        entry = web.comfy(f'/history/{pid}').get(pid) or {}
    except Exception:
        return None, None
    start = end = None
    for msg in (entry.get('status') or {}).get('messages') or []:
        if isinstance(msg, (list, tuple)) and len(msg) == 2:
            kind, data = msg
            if kind == 'execution_start':
                start = data.get('timestamp', data.get('execution_start'))
            elif kind == 'execution_success':
                end = data.get('timestamp', data.get('execution_success'))
    return start, end


def cmd_render(args):
    manifest = load_manifest(args.id)
    only = set(args.only.split(',')) if args.only else set()
    retry = set(args.retry_slot.split(',')) if args.retry_slot else set()
    named = only | retry
    bad = named - set(manifest['slots'])
    if bad:
        raise PilotError(f'unknown slot id(s): {", ".join(sorted(bad))}')

    readiness.forget()
    check = readiness.check('video', refresh=True)
    if check['level'] == 'unavailable':
        print(f'render: refused, video generation is not ready: {check["reason"]}', file=sys.stderr)
        return 2

    failures = 0
    for slot_id in manifest['order']:
        if only and slot_id not in only:
            continue
        slot = manifest['slots'][slot_id]
        prev = slot['render']
        if prev and prev['state'] == 'unknown' and slot_id not in retry:
            print(f'render: {slot_id}: its last attempt is recorded UNKNOWN - ComfyUI may still '
                  f'be executing prompt {", ".join(prev["prompt_ids"])}; the pilot run stops. '
                  f'Resolve that job in the queue UI first, then --retry-slot {slot_id}.',
                  file=sys.stderr)
            return 3
        if prev and prev['state'] in ('finished', 'failed') and slot_id not in retry:
            print(f'render: {slot_id}: {prev["state"]} already recorded; resume skips it '
                  f'(--retry-slot {slot_id} to run it again explicitly)')
            failures += prev['state'] == 'failed'
            continue
        p = slot['prompt']
        if not p or 'text' not in p:
            failures += 1
            print(f'render: {slot_id}: no prompt text (the prompt call '
                  f'{"failed" if p else "was never run"}); slot stays unrendered - '
                  f'run `pilot.py prompts --id {args.id}` first', file=sys.stderr)
            continue

        scene_id = slot['scene']
        settings = manifest['settings']
        args_graph = dict(
            prompt=p['text'], seconds=settings['seconds_per_scene'][scene_id],
            width=settings['width'], height=settings['height'], seed=slot['seed'],
            refs=[(fid, Path(manifest['fixtures'][fid]['path']).read_bytes())
                  for fid in next(s for s in manifest['scenes'] if s['id'] == scene_id)['resolved_refs']],
            first=None, last=None, two_pass=settings['two_pass'], loras=None)
        job = web.new_job('render-pilot', ['render'], project=None, scene=slot_id)
        print(f'render: {slot_id}: submitting ({slot["arm"]} prompt, seed {slot["seed"]})...')
        submitted = time.time()
        web.run_job(job, web.do_render_adhoc, args_graph, False)   # finishing OFF; ONE at a time
        wall = time.time() - submitted
        rec = {'state': job['state'], 'job_id': job['id'], 'prompt_ids': list(job['prompt_ids']),
               'correlation': job.get('correlation'), 'submitted_at': submitted,
               'hold_seconds': round((job.get('started') or submitted) - job['created'], 1),
               'wall_seconds': round(wall, 1),
               'started_at': None, 'ended_at': None, 'backend_seconds': None,
               'queue_wait_seconds': None,
               'take': (job.get('take') or {}).get('path'), 'error': job.get('error'),
               'at': _utc()}
        if rec['prompt_ids']:
            start_ms, end_ms = history_times(rec['prompt_ids'][-1])
            if start_ms:
                rec['started_at'] = start_ms / 1000
                rec['queue_wait_seconds'] = round(start_ms / 1000 - submitted, 1)
            if start_ms and end_ms and end_ms >= start_ms:
                rec['ended_at'] = end_ms / 1000
                rec['backend_seconds'] = round((end_ms - start_ms) / 1000, 1)
            elif job['state'] == 'finished':
                print('render: note: this ComfyUI left no execution_start/execution_success '
                      'stamps in /history; backend time is null for this slot (see the '
                      'real-backend checklist in docs/pilot.md)')
        slot['render'] = rec
        save_manifest(manifest)                    # resumable: the slot verdict is on disk at once
        if rec['state'] == 'finished':
            print(f'render: {slot_id}: finished; backend {rec["backend_seconds"]} s, '
                  f'wall {rec["wall_seconds"]} s (one take, finishing off)')
        elif rec['state'] == 'failed':
            failures += 1
            print(f'render: {slot_id}: FAILED ({rec["error"]}); recorded, never auto-resubmitted '
                  f'(--retry-slot {slot_id} is the explicit way back)', file=sys.stderr)
        elif rec['state'] == 'unknown':
            print(f'render: {slot_id}: state UNKNOWN - ComfyUI may still be executing prompt '
                  f'{", ".join(rec["prompt_ids"])}; STOPPING the pilot run. Nothing will be '
                  f'submitted until that job is resolved in the queue UI (it is in jobs.db and '
                  f'the queue page, reconciled by the running UI or a restart).', file=sys.stderr)
            return 3
    return 1 if failures else 0


# ---------------------------------------------------------------- review-prepare

CLIP_NAME = re.compile(r'^clip-[0-9]{2}\.mp4$')


def _reencode(ffmpeg, src, dest):
    """One identical re-encode path for EVERY clip: same codec, preset, pixel format, container,
    and all metadata stripped (-map_metadata -1 plus +bitexact so the muxer writes no encoder
    tag either). Identical for both arms, so neither container settings nor tags can reveal one."""
    cmd = [ffmpeg, '-v', 'error', '-y', '-i', str(src), '-map', '0',
           '-map_metadata', '-1', '-map_chapters', '-1',
           '-fflags', '+bitexact', '-flags:v', '+bitexact',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(dest)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise PilotError(f'ffmpeg re-encode of {src} failed: {(res.stderr or res.stdout).strip()}')


REVIEW_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Take review</title>
<style>
 body{font-family:system-ui,sans-serif;background:#14161a;color:#e6e8ea;max-width:56em;margin:1.5em auto;padding:0 1em}
 h1{font-size:1.2em} .sub{color:#9aa0a8;font-size:.9em;margin-bottom:1.5em}
 section{border:1px solid #2c3138;border-radius:8px;padding:1em;margin:0 0 1.2em}
 video{width:100%;background:#000}
 fieldset{border:1px solid #2c3138;border-radius:6px;margin:.6em 0;padding:.4em .8em}
 legend{font-size:.9em;color:#c8cdd4}
 label{margin-right:.9em} .saved{font-size:.85em;color:#7fbf7f;margin-left:.6em}
 textarea{width:100%;min-height:3em;background:#1e2126;color:#e6e8ea;border:1px solid #2c3138;border-radius:6px}
 #progress{position:sticky;top:0;background:#14161a;padding:.4em 0;font-size:.9em;color:#c8cdd4}
</style>
<h1>Blinded take review</h1>
<p class="sub">Rate each clip 1-5 on five things (1 = fails it, 5 = excellent), accept or
reject the take, note anything you noticed. Arrow keys move inside a scale, Tab moves between
fields; each change saves by itself. Reload at any time: what you rated comes back.</p>
<div id="progress"></div>
%SECTIONS%
<script>
const CLIPS = %CLIPS%;
const CRITERIA = %CRITERIA%;
function els(clip){
  const out = {scores:{}, verdict:null, notes:null};
  CRITERIA.forEach(c=>{const r=document.querySelector('input:checked[name="'+clip+'|'+c+'"]');
    if(r) out.scores[c]=Number(r.value);});
  const v=document.querySelector('input:checked[name="'+clip+'|verdict"]');
  if(v) out.verdict=v.value;
  out.notes=document.getElementById(clip+'|notes').value;
  return out;
}
function counter(){
  let rated=0;
  CLIPS.forEach(c=>{const e=els(c); if(e.verdict && CRITERIA.every(k=>e.scores[k])) rated++;});
  document.getElementById('progress').textContent='takes fully rated: '+rated+' / '+CLIPS.length;
}
function save(clip, statusId){
  const el=document.getElementById(statusId);
  fetch('api/ratings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({clip:clip},els(clip)))})
    .then(r=>{el.textContent=r.ok?'saved':'save failed';})
    .catch(()=>{el.textContent='not saved - run pilot.py review-serve';});
  counter();
}
document.querySelectorAll('input[name*="|"], textarea').forEach(el=>{
  el.addEventListener('change',()=>{
    const clip=el.name?el.name.split('|')[0]:el.id.split('|')[0];
    save(clip, clip+'|status');
  });
});
fetch('api/ratings').then(r=>r.ok?r.json():{clips:{}}).then(doc=>{
  Object.entries(doc.clips||{}).forEach(([clip,rec])=>{
    Object.entries(rec.scores||{}).forEach(([c,v])=>{
      const i=document.querySelector('input[name="'+clip+'|'+c+'"][value="'+v+'"]'); if(i) i.checked=true;});
    if(rec.verdict){const i=document.querySelector('input[name="'+clip+'|verdict"][value="'+rec.verdict+'"]'); if(i) i.checked=true;}
    if(rec.notes!=null) document.getElementById(clip+'|notes').value=rec.notes;
  });
  counter();
}).catch(()=>{counter();});
</script>
"""


def cmd_review_prepare(args):
    manifest = load_manifest(args.id)
    root = run_dir(args.id)
    review = root / 'review'
    done = [s for s in manifest['order']
            if (manifest['slots'][s].get('render') or {}).get('state') == 'finished'
            and Path(manifest['slots'][s]['render']['take']).is_file()]
    if not done:
        raise PilotError('no finished takes with output files; run `pilot.py render` first')
    if len(done) != 12:
        print(f'review-prepare: note: only {len(done)} of 12 slots finished; the review covers '
              f'those - the report will count the rest as unaccepted/unrated', file=sys.stderr)
    seed = args.seed if args.seed is not None else random.randrange(1 << 31)
    rng = random.Random(seed)
    clips_order = sorted(done)
    rng.shuffle(clips_order)
    review.mkdir(parents=True, exist_ok=True)
    ffmpeg = config.value('ffmpeg')
    key = {'kind': 'freevideo-pilot-key', 'seed': seed, 'created': _utc(), 'clips': {}}
    for i, slot_id in enumerate(clips_order, 1):
        name = f'clip-{i:02d}.mp4'
        _reencode(ffmpeg, manifest['slots'][slot_id]['render']['take'], review / name)
        key['clips'][name] = {'slot': slot_id, 'scene': manifest['slots'][slot_id]['scene'],
                              'arm': manifest['slots'][slot_id]['arm'],
                              'seed': manifest['slots'][slot_id]['seed'],
                              'take': manifest['slots'][slot_id]['render']['take']}
    (root / 'key.json').write_text(json.dumps(key, indent=1) + '\n')   # outside review/, never served

    names = [f'clip-{i:02d}.mp4' for i in range(1, len(clips_order) + 1)]
    sections = []
    for n, name in enumerate(names, 1):
        clip = name[:-4]                                   # ids on the page are stems
        fields = []
        for c in CRITERIA:
            radios = ''.join(
                f'<label><input type="radio" name="{clip}|{c}" value="{v}"> {v}</label>'
                for v in range(1, 6))
            fields.append(f'<fieldset><legend>{CRITERION_LABEL[c]}</legend>{radios}</fieldset>')
        verdict = ('<label><input type="radio" name="%s|verdict" value="accepted"> Accept</label>'
                   '<label><input type="radio" name="%s|verdict" value="rejected"> Reject</label>'
                   % (clip, clip))
        fields.append(f'<fieldset><legend>Take</legend>{verdict}'
                      f'<span class="saved" id="{clip}|status"></span></fieldset>')
        fields.append(f'<label for="{clip}|notes">Notes</label>'
                      f'<textarea id="{clip}|notes"></textarea>')
        sections.append(f'<section data-clip="{clip}"><h2>Take {n} of {len(names)} '
                        f'({clip})</h2><video controls preload="metadata" src="{name}"></video>'
                        + ''.join(fields) + '</section>')
    html = (REVIEW_HTML
            .replace('%SECTIONS%', ''.join(sections))
            .replace('%CLIPS%', json.dumps([n[:-4] for n in names]))
            .replace('%CRITERIA%', json.dumps(list(CRITERIA))))
    (review / 'index.html').write_text(html)
    print(f'review-prepare: {len(names)} blinded clips in {review}/, page index.html, '
          f'KEY in {root / "key.json"} (never served)')
    print(f'review-prepare: clip numbering came from seed {seed}; the same seed reproduces '
          f'the same numbering. Reviews must not open key.json.')
    return 0


# a wrapper so review-prepare can record an unseeded run's seed without changing the RNG use
def review_prepare(args):
    if args.seed is None:
        args.seed = random.randrange(1 << 31)
    return cmd_review_prepare(args)


# ---------------------------------------------------------------- review-serve

def load_ratings(pilot_id):
    path = run_dir(pilot_id) / 'ratings.json'
    if path.is_file():
        return json.loads(path.read_text())
    return {'kind': 'freevideo-pilot-ratings', 'schema_version': SCHEMA_VERSION, 'log': []}


def current_ratings(ratings):
    """The newest record per clip derived from the append-only log."""
    out = {}
    for rec in ratings['log']:
        out[rec['clip']] = rec
    return out


def append_rating(pilot_id, rec):
    """Append-only history written with a whole-file atomic replace (tmp + os.replace): a crash
    leaves either the old or the new file, never a half-written log."""
    lock = _RATINGS_LOCKS.setdefault(pilot_id, threading.Lock())
    with lock:
        ratings = load_ratings(pilot_id)
        ratings['log'].append(rec)
        path = run_dir(pilot_id) / 'ratings.json'
        tmp = path.with_name('.ratings.tmp')
        tmp.write_text(json.dumps(ratings, indent=1) + '\n')
        os.replace(tmp, path)
        return len(ratings['log'])


_RATINGS_LOCKS = {}


def _valid_post(body, known_clips):
    clip = body.get('clip')
    if clip not in known_clips:
        return None, f'unknown clip {clip!r}'
    scores = body.get('scores') or {}
    if set(scores) - set(CRITERIA):
        return None, 'unknown criterion in scores'
    for v in scores.values():
        if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5):
            return None, 'scores must be integers 1-5'
    verdict = body.get('verdict')
    if verdict not in (None, 'accepted', 'rejected'):
        return None, 'verdict must be accepted/rejected/None'
    notes = body.get('notes') or ''
    if not isinstance(notes, str) or len(notes) > 4000:
        return None, 'notes must be a string up to 4000 chars'
    return {'clip': clip, 'scores': {c: scores[c] for c in CRITERIA if c in scores},
            'verdict': verdict, 'notes': notes, 'at': _utc()}, None


def review_server(pilot_id, host, port):
    """The blinded review server: static review/ + the ratings API. Binds the config host
    (127.0.0.1 by default); it never serves key.json or anything outside review/."""
    review = run_dir(pilot_id) / 'review'
    key_path = run_dir(pilot_id) / 'key.json'
    if not (review / 'index.html').is_file():
        raise PilotError('no review/index.html; run `pilot.py review-prepare` first')
    if not key_path.is_file():
        raise PilotError('no key.json; run `pilot.py review-prepare` first')
    # the key's clip ids are filenames; the page/POST use the extensionless id
    known = {c[:-4] if str(c).endswith('.mp4') else str(c)
             for c in json.loads(key_path.read_text())['clips']}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def _send(self, code, raw, ctype):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in ('/', '/index.html'):
                return self._send(200, (review / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/ratings':
                return self._send(200, json.dumps({'clips': current_ratings(load_ratings(pilot_id))}).encode(),
                                  'application/json')
            name = path.lstrip('/')
            if CLIP_NAME.fullmatch(name) and (review / name).is_file():
                return self._send(200, (review / name).read_bytes(), 'video/mp4')
            return self._send(404, b'not found', 'text/plain')

        def do_POST(self):
            if self.path.split('?', 1)[0] != '/api/ratings':
                return self._send(404, b'not found', 'text/plain')
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            except ValueError:
                return self._send(400, b'not json', 'text/plain')
            rec, err = _valid_post(body, known)
            if err:
                return self._send(400, err.encode(), 'text/plain')
            n = append_rating(pilot_id, rec)
            return self._send(200, json.dumps({'saved': True, 'entries': n}).encode(),
                              'application/json')

    srv = ThreadingHTTPServer((host, port), Handler)
    return srv


def cmd_review_serve(args):
    host = args.host or config.value('ui_host')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'review-serve: binding {host} on purpose (--host); default stays 127.0.0.1')
    srv = review_server(args.id, host, args.port or config.value('pilot_port'))
    print(f'review-serve: blinded review at http://{srv.server_address[0]}:{srv.server_address[1]}/ '
          f'- one reviewer, no arm information on this page; stop with Ctrl-C')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nreview-serve: stopped; ratings.json holds the append-only history')
    return 0


# ---------------------------------------------------------------- report

def _clip_ratings_complete(current, clips):
    """Every clip of the key rated: all five scores AND an accept/reject verdict. Returns the
    list of {clip, missing} gaps."""
    gaps = []
    for clip in clips:
        rec = current.get(clip)
        if not rec:
            gaps.append({'clip': clip, 'missing': ['all']})
            continue
        missing = [c for c in CRITERIA if not isinstance(rec.get('scores', {}).get(c), int)]
        if rec.get('verdict') not in ('accepted', 'rejected'):
            missing.append('accept/reject')
        if missing:
            gaps.append({'clip': clip, 'missing': missing})
    return gaps


def _stats_block(slots, ratings_by_slot):
    """Acceptance, criterion means and time-per-accepted-take for one subset of slots. Failed
    and unrendered slots spend their time with zero accepted takes; both time columns are kept
    strictly apart and carry their own n."""
    rated = [s for s in slots if s['slot'] in ratings_by_slot]
    accepted = [s for s in rated if ratings_by_slot[s['slot']]['verdict'] == 'accepted']
    crit = {}
    for c in CRITERIA:
        vals = [ratings_by_slot[s['slot']]['scores'][c] for s in rated
                if isinstance(ratings_by_slot[s['slot']].get('scores', {}).get(c), int)]
        crit[c] = {'n': len(vals), 'values': vals,
                   'mean': round(sum(vals) / len(vals), 2) if vals else None,
                   'min': min(vals, default=None), 'max': max(vals, default=None)}
    backend = [s['render']['backend_seconds'] for s in slots
               if (s.get('render') or {}).get('backend_seconds') is not None]
    wall = [s['render']['wall_seconds'] for s in slots if (s.get('render') or {}).get('wall_seconds')]
    prompt = [s['prompt']['seconds_spent'] for s in slots
              if (s.get('prompt') or {}).get('seconds_spent') is not None]
    n_acc = len(accepted)

    def per(values):
        return round(sum(values) / n_acc, 1) if n_acc and values else None
    return {
        'slots': len(slots), 'finished': sum(1 for s in slots if (s.get('render') or {}).get('state') == 'finished'),
        'failed_slots': sum(1 for s in slots if (s.get('render') or {}).get('state') == 'failed'),
        'rated': len(rated), 'accepted': n_acc,
        'acceptance_rate': round(n_acc / len(rated), 3) if rated else None,
        'criteria': crit,
        'time': {
            'backend_seconds_sum': round(sum(backend), 1), 'backend_n': len(backend),
            'per_accepted_take_backend_seconds': per(backend),
            'wall_seconds_sum': round(sum(wall), 1), 'wall_n': len(wall),
            'per_accepted_take_wall_seconds': per(wall),
            'prompt_seconds_sum': round(sum(prompt), 1) if prompt else 0.0,
            'per_accepted_take_wall_plus_prompt_seconds': per(wall + prompt) if prompt else None,
        },
    }


def cmd_report(args):
    manifest = load_manifest(args.id)
    root = run_dir(args.id)
    key_path = root / 'key.json'
    if not key_path.is_file():
        raise PilotError('no key.json; run `pilot.py review-prepare` first')
    key = json.loads(key_path.read_text())
    ratings = load_ratings(args.id)
    current = current_ratings(ratings)
    gaps = _clip_ratings_complete(current, sorted(key['clips']))
    if gaps and not args.partial:
        print('report: REFUSED - not every clip is rated. Missing:', file=sys.stderr)
        for g in gaps:
            print(f'  {g["clip"]}: {", ".join(g["missing"])}', file=sys.stderr)
        print('report: rate them (review-serve) or pass --partial for a clearly labelled '
              'partial report.', file=sys.stderr)
        return 2

    # unblind now, and only now: slot -> arm/scene/seed from the key
    by_slot = {}
    for clip, info in key['clips'].items():
        rec = current.get(clip)
        if rec:
            by_slot[info['slot']] = rec
    slots = list(manifest['slots'].values())
    arms = {arm: _stats_block([s for s in slots if s['arm'] == arm], by_slot) for arm in ARMS}
    scenes = {sc['id']: _stats_block([s for s in slots if s['scene'] == sc['id']], by_slot)
              for sc in manifest['scenes']}

    doc = {
        'kind': 'freevideo-pilot-report', 'id': manifest['id'], 'partial': bool(gaps),
        'created': _utc(), 'caution': CAUTION, 'small_n': _SMALL_N,
        'versions': manifest['versions'], 'settings': manifest['settings'],
        'arms': arms, 'scenes': scenes,
        'ratings_history_entries': len(ratings['log']),
        'clips_missing_ratings': gaps,
    }
    tmp = root / '.report.json.tmp'
    tmp.write_text(json.dumps(doc, indent=1) + '\n')
    os.replace(tmp, root / 'report.json')

    lines = [f'# Pilot {manifest["id"]}: blinded review report', '']
    if gaps:
        lines += ['> **PARTIAL REPORT - not final.** Clips still missing ratings: '
                  + ', '.join(g['clip'] for g in gaps) + '.', '']
    lines += [CAUTION, '', _SMALL_N, '']
    lines += ['Finishing was OFF for every slot: these are raw generations, no upscaling or '
              'frame interpolation touched them.', '']
    for title, table in (('per arm', arms), ('per scene', scenes)):
        lines += [f'## {title}', '',
                  '| group | rated / slots | accepted | acceptance | backend s/accepted take | '
                  'wall s/accepted take |',
                  '|---|---|---|---|---|---|']
        for name, st in table.items():
            t = st['time']
            lines.append(f"| {name} | {st['rated']} / {st['slots']} | {st['accepted']} | "
                         f"{st['acceptance_rate'] if st['acceptance_rate'] is not None else '-'} "
                         f"(of {st['rated']}) | {t['per_accepted_take_backend_seconds'] if t['per_accepted_take_backend_seconds'] is not None else '-'} "
                         f"(n={t['backend_n']}) | {t['per_accepted_take_wall_seconds'] if t['per_accepted_take_wall_seconds'] is not None else '-'} "
                         f"(n={t['wall_n']}) |")
            if name == 'director':
                lines.append(f"| director (wall + prompt time) | | | | - | "
                             f"{t['per_accepted_take_wall_plus_prompt_seconds'] if t['per_accepted_take_wall_plus_prompt_seconds'] is not None else '-'} |")
            lines.append(f"| criteria: " + ' | '.join(
                f"{CRITERION_LABEL[c]} mean {st['criteria'][c]['mean']} "
                f"[{st['criteria'][c]['min']}-{st['criteria'][c]['max']}, n={st['criteria'][c]['n']}]"
                for c in CRITERIA) + ' | | | | | |')
        lines.append('')
    lines += ['Backend time is ComfyUI execution_start to execution_success (queue wait and '
              'every upload excluded); wall time is submit to output file ready, including '
              'queue wait; failed slots count their time in both with zero accepted takes. '
              'For the director arm the prompt-writing time is shown as its own third column '
              'and never folded silently into the render time.', '',
              'No prompt length, keyword match or format-compliance number appears in this '
              'report by design: ' + CAUTION]
    tmp = root / '.report.md.tmp'
    tmp.write_text('\n'.join(lines) + '\n')
    os.replace(tmp, root / 'report.md')
    print(f'report: wrote {root / "report.md"} and {root / "report.json"}'
          + (' (PARTIAL)' if gaps else ''))
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    config.add_service_args(p, names=('comfy_url', 'comfy_root', 'jobs_db_file', 'stats_file',
                                      'director_url', 'director_model', 'eval_fixture_dir',
                                      'pilot_portrait_image'))
    sub = p.add_subparsers(dest='cmd', required=True)

    q = sub.add_parser('plan', help='write the immutable 12-slot manifest')
    q.add_argument('--id', required=True)
    q.add_argument('--scenes', default=str(SCENES_DEFAULT))
    q.add_argument('--seeds', default='11,22')
    q.add_argument('--order-seed', type=int, default=None,
                   help='seed for the randomised render order (recorded; default: random)')
    q.add_argument('--fixture-dir', default=str(config.value('eval_fixture_dir')))
    q.add_argument('--width', type=int, default=1344)
    q.add_argument('--height', type=int, default=768)
    q.add_argument('--two-pass', action='store_true', default=True)
    q.add_argument('--no-two-pass', dest='two_pass', action='store_false')

    q = sub.add_parser('prompts', help='record both arms\' prompts (arm B through director.py)')
    q.add_argument('--id', required=True)
    q.add_argument('--redo-slot', help='regenerate exactly this slot\'s prompt')

    q = sub.add_parser('render', help='run the 12 slots one at a time via web.py\'s tracked submit path')
    q.add_argument('--id', required=True)
    q.add_argument('--only', help='comma-separated slot ids to render this run')
    q.add_argument('--retry-slot', help='explicitly resubmit exactly these finished/failed slots')

    q = sub.add_parser('review-prepare', help='write the blinded clips, the review page and the key')
    q.add_argument('--id', required=True)
    q.add_argument('--seed', type=int, default=None,
                   help='recording-numbering seed (recorded in key.json; default: random)')

    q = sub.add_parser('review-serve', help='serve the blinded review page on 127.0.0.1')
    q.add_argument('--id', required=True)
    q.add_argument('--host', default=None, help='default 127.0.0.1 (config ui_host)')
    q.add_argument('--port', type=int, default=None, help='default config pilot_port (8392)')

    q = sub.add_parser('report', help='unblind and report acceptance, scores and times')
    q.add_argument('--id', required=True)
    q.add_argument('--partial', action='store_true', help='print a clearly labelled partial report')

    args = p.parse_args(argv)
    dispatch = {'plan': plan, 'prompts': cmd_prompts, 'render': cmd_render,
                'review-prepare': review_prepare, 'review-serve': cmd_review_serve,
                'report': cmd_report}
    try:
        return dispatch[args.cmd](args)
    except (PilotError, director.DirectorError, readiness.NotReady) as e:
        print(f'pilot: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
