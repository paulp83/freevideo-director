#!/usr/bin/env python3
"""FreeVideo director: turn a rough idea (+ optional images) into a MiniMax H3 prompt.

Uses the official MiniMax-H3 `h3-prompt-writing` skill (skill/, pinned copy) as the
system prompt and a local OpenAI-compatible vision model (default: the
freevideo-director vLLM container on GPU 1, :8078). Stdlib only.

  director "two friends duel with lightsabers in a parking lot" --ref group.jpg
  director "a fox runs toward camera at dawn" --seconds 6
  director "she turns and walks into the sea" --first start.png --last end.png

A browser UI over the same function lives in web.py.
"""
import argparse, base64, json, os, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

import h3check

HERE = Path(__file__).resolve().parent
SKILL = HERE / 'skill'
# Director backends on GPU 1, in order of preference (see README / eval/). Whichever answers is used.
# DIRECTOR_URL (+ DIRECTOR_MODEL) pins one explicitly.
BACKENDS = [('http://127.0.0.1:8144/v1/chat/completions', 'qwen3.8-27b'),       # sgl single, best scripts
            ('http://127.0.0.1:8078/v1/chat/completions', 'qwen3.6-35b-a3b')]   # vllm single nvfp4, fastest


def backend():
    """Return (url, model) of the first director backend that is up."""
    if os.environ.get('DIRECTOR_URL'):
        return os.environ['DIRECTOR_URL'], os.environ.get('DIRECTOR_MODEL', 'default')
    for url, model in BACKENDS:
        try:
            with urllib.request.urlopen(url.replace('/chat/completions', '/models'), timeout=2):
                return url, model
        except OSError:
            continue
    raise DirectorError('no director model is up: start one (docker start director-qwen38-27b '
                        'or docker start freevideo-director)')

MODES = {
    't2va': ('base-en.txt', 'T2VA (text only, no keyframes)'),
    'i2va': ('base-en.txt', 'I2VA (Picture 1 is the first frame)'),
    'l2va': ('base-en.txt', 'L2VA (Picture 1 is the last frame)'),
    'fl2va': ('base-en.txt', 'FL2VA (Picture 1 is the first frame, Picture 2 the last frame)'),
    'ref2va': ('ref-en.txt', 'full-reference Ref2VA (the pictures are reference material, not keyframes)'),
}


class DirectorError(Exception):
    pass


def image_part(path, max_side):
    """Downscale to max_side (ffmpeg) and return an OpenAI image_url content part.

    `path` is a file path or raw image bytes."""
    if isinstance(path, (bytes, bytearray)):
        with tempfile.NamedTemporaryFile(suffix='.img') as src:
            src.write(path); src.flush()
            return image_part(src.name, max_side)
    path = Path(path).expanduser()
    if not path.is_file():
        raise DirectorError(f'image not found: {path}')
    with tempfile.NamedTemporaryFile(suffix='.jpg') as tmp:
        scale = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-vf', scale, '-q:v', '3', tmp.name],
                       check=True)
        data = base64.b64encode(Path(tmp.name).read_bytes()).decode()
    return {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{data}'}}


def select_mode(refs=(), first=None, last=None):
    if refs and (first or last):
        raise DirectorError('use either reference images or first/last frames, not both')
    if len(refs) > 2:
        raise DirectorError('at most 2 images (server limit)')
    return ('ref2va' if refs else 'fl2va' if first and last else
            'i2va' if first else 'l2va' if last else 't2va')


def chat(url, model, system, content, think, temperature, max_tokens=12000, extra=()):
    """POST one chat completion; return (text, completion_tokens). `extra` are messages after the user turn."""
    body = {'model': model, 'temperature': temperature, 'max_tokens': max_tokens,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}, *extra],
            'chat_template_kwargs': {'enable_thinking': think}}
    req = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            reply = json.load(r)
    except OSError as e:
        raise DirectorError(f'request to {url} failed: {e}. Is the director model up? '
                            '(docker ps | grep freevideo-director)') from e
    text = reply['choices'][0]['message'].get('content') or ''
    text = re.sub(r'(?s)^.*?</think>', '', text).strip()          # reasoning left inline, if any
    return text, reply.get('usage', {}).get('completion_tokens')


def direct(idea, seconds=10, refs=(), first=None, last=None, think=True, temperature=0.7,
           max_side=1024, url=None, model=None, retries=1):
    """Return (mode, prompt, completion_tokens, model). Images are paths or raw bytes.

    A reply that fails the h3check format checks is sent back once (`retries`) with the failed check names."""
    if not idea.strip():
        raise DirectorError('describe the idea first')
    if not 4 <= seconds <= 15:
        raise DirectorError('length must be 4-15 seconds (H3 range)')
    mode = select_mode(refs, first, last)
    if not url:
        url, model = backend()
    guide, label = MODES[mode]
    images = list(refs) or [x for x in (first, last) if x]

    system = ('You are a film director who writes MiniMax H3 video prompts. Follow this skill and its '
              'reference guide exactly: field names, section order, labels, shot timing notation.\n\n'
              + (SKILL / 'SKILL.md').read_text() + '\n\n--- references/' + guide + ' ---\n\n'
              + (SKILL / 'references' / guide).read_text())
    user = (f'Mode: {label}.\n'
            f'Target duration: {seconds:g} seconds. Every shot timestamp must fall inside it, and the '
            'timeline must fill it.\n')
    if images:
        user += (f'{len(images)} image(s) attached, in order: '
                 + ', '.join(f'<Picture {i + 1}>' for i in range(len(images)))
                 + '. Use exactly these labels; the video tool maps them to the uploaded files. Describe '
                 'the people and objects you take from them concretely (clothing, hair, colors, build) '
                 'so they stay recognizable.\n')
    user += ('Idea from the user:\n' + idea + '\n\n'
             'Output ONLY the final prompt text, exactly as it should be pasted into the video tool: '
             'the alignment instruction line first if this mode has one, then the sections. '
             'No markdown fences, no headings, no commentary.')

    content = [{'type': 'text', 'text': user}] + [image_part(x, max_side) for x in images]
    text, tokens = chat(url, model, system, content, think, temperature)
    text = re.sub(r'^```\w*\n|\n```$', '', text).strip()
    if not text:
        raise DirectorError('empty reply (try again, or turn reasoning off)')
    bad = h3check.failures(mode, text, seconds, len(images))
    for _ in range(retries if bad else 0):
        fix = ('Your reply failed these format checks: ' + ', '.join(bad) + '. Fix them and output ONLY the '
               'corrected final prompt text, exactly as before: no markdown fences, no commentary.')
        again, more = chat(url, model, system, content, think, temperature,
                           extra=[{'role': 'assistant', 'content': text}, {'role': 'user', 'content': fix}])
        again = re.sub(r'^```\w*\n|\n```$', '', again).strip()
        tokens = (tokens or 0) + (more or 0)
        again_bad = h3check.failures(mode, again, seconds, len(images)) if again else bad + ['empty']
        if len(again_bad) <= len(bad):                        # keep the better reply; a tie takes the later one
            text, bad = again or text, again_bad
    return mode, text, tokens, model


STORYBOARD_SYSTEM = """You are a film director planning a short film that will be generated scene by scene with the
MiniMax H3 video model. Each scene is rendered independently, is 4-15 seconds long, and has its own sound.
Plan the film from the user's logline, total length and any reference pictures.

Return ONLY one JSON object, no prose, no markdown fences:
{
  "title": "short film title",
  "bible": "style bible: every recurring character by a short name with exact, concrete look (age, build, hair, face, wardrobe colours and items), the location(s), time of day, lighting and colour palette, camera style, overall mood. If reference pictures are given, describe the people/places in them concretely and say which <Picture N> they come from.",
  "music": "one line describing the score for the film's music bed (genre, instruments, tempo, mood arc)",
  "scenes": [
    {"title": "2-4 words", "seconds": 8, "connect": "cut",
     "action": "2-4 sentences: what visibly happens, shot size and camera movement, key sounds or a short line of dialogue in quotes"}
  ]
}

Rules:
- The scene seconds MUST add up to the total length; each scene 4-15 s (integers). Prefer 6-10 s scenes.
- 3 to 8 scenes. Give the film a beginning, middle and end; vary shot sizes and angles between scenes.
- connect is "cut" (a new shot) or "continue" (the action flows on from the exact last moment of the previous
  scene, same place and characters, no time jump). The first scene is always "cut". Use "continue" sparingly.
- Keep characters' looks identical across scenes; refer to them by the names defined in the bible.
- No background music in scene actions; the score is added in editing."""

CHARACTERS_HEAD = 'Recurring characters (keep exactly these looks):'


def _json_object(text):
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise DirectorError('the director did not return a storyboard (no JSON found)')
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise DirectorError(f'storyboard JSON did not parse: {e}') from e


def _read_plan(text):
    """(plan, scenes) from a storyboard reply; DirectorError when there is no JSON or no scene."""
    plan = _json_object(text)
    scenes = [s for s in plan.get('scenes', []) if isinstance(s, dict) and s.get('action')]
    if not scenes:
        raise DirectorError('the storyboard has no scenes')
    return plan, scenes


def _fit_durations(scenes, total):
    """Clamp scene lengths to 4-15 s and make them sum to `total` (when possible)."""
    secs = [min(15, max(4, int(round(float(s.get('seconds') or 8))))) for s in scenes]
    for _ in range(500):
        diff = total - sum(secs)
        if diff == 0:
            break
        order = sorted(range(len(secs)), key=lambda i: secs[i], reverse=diff < 0)
        for i in order:
            if (diff > 0 and secs[i] < 15) or (diff < 0 and secs[i] > 4):
                secs[i] += 1 if diff > 0 else -1
                break
        else:
            break
    for s, n in zip(scenes, secs):
        s['seconds'] = n
    return scenes


def storyboard(logline, seconds=30, refs=(), aspect='16:9', think=True, url=None, model=None, character_notes=''):
    """Plan a multi-scene film. Returns (plan dict, model). Images are paths or raw bytes."""
    if not logline.strip():
        raise DirectorError('describe the film first')
    if not url:
        url, model = backend()
    user = (f'Logline: {logline}\nTotal length: {int(seconds)} seconds.\nAspect: {aspect}.\n'
            + (f'{len(refs)} reference picture(s) attached, in order: '
               + ', '.join(f'<Picture {i + 1}>' for i in range(len(refs))) + '.\n' if refs else '')
            + (f'{CHARACTERS_HEAD}\n{character_notes.strip()}\n' if character_notes.strip() else '')
            + 'Return the JSON storyboard now.')
    content = [{'type': 'text', 'text': user}] + [image_part(x, 1024) for x in refs]
    text, _ = chat(url, model, STORYBOARD_SYSTEM, content, think, 0.7)
    try:
        plan, scenes = _read_plan(text)
    except DirectorError as e:                                    # one corrective retry, then give up
        fix = f'That reply was not usable ({e}). Return ONLY the corrected JSON storyboard object.'
        text, _ = chat(url, model, STORYBOARD_SYSTEM, content, think, 0.7,
                       extra=[{'role': 'assistant', 'content': text or '(empty reply)'},
                              {'role': 'user', 'content': fix}])
        plan, scenes = _read_plan(text)
    for i, s in enumerate(scenes):
        s['title'] = str(s.get('title') or f'Scene {i + 1}')[:60]
        s['connect'] = 'continue' if i and s.get('connect') == 'continue' else 'cut'
        s['action'] = str(s['action'])
    plan['scenes'] = _fit_durations(scenes, int(seconds))
    plan['title'] = str(plan.get('title') or 'Untitled film')
    plan['bible'] = str(plan.get('bible') or '')
    plan['music'] = str(plan.get('music') or '')
    return plan, model


def scene_idea(film, index):
    """The idea text the director turns into scene `index`'s H3 prompt (bible-anchored)."""
    scenes = film['scenes']
    scene = scenes[index]
    lines = [f'Scene {index + 1} of {len(scenes)} of the short film "{film.get("title", "")}".',
             'Style bible (keep every character, wardrobe, place and palette exactly as described):',
             film.get('bible', ''), '']
    if (film.get('character_notes') or '').strip():
        lines += [CHARACTERS_HEAD, film['character_notes'].strip(), '']
    lines.append(f'This scene ({scene["seconds"]} s): {scene["action"]}')
    if scene.get('connect') == 'continue' and index:
        lines.append('This scene continues directly from the previous scene, which ended with: '
                     + scenes[index - 1]['action'])
    elif index:
        lines.append('This is a new shot (hard cut) after the previous scene.')
    lines.append('Sound: diegetic sound effects and dialogue only. The film score is added in editing, so '
                 'write non_diegetic_music: None.')
    return '\n'.join(lines)


def save(text, out=None):
    out = Path(out) if out else HERE / 'out' / time.strftime('%Y%m%d-%H%M%S.txt')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + '\n')
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('idea', help='Rough idea, in any language')
    p.add_argument('--seconds', type=float, default=10, help='Target length, 4-15 s (default 10)')
    p.add_argument('--ref', action='append', default=[], help='Reference image (Ref2VA); up to 2')
    p.add_argument('--first', help='First-frame image (I2VA / FL2VA)')
    p.add_argument('--last', help='Last-frame image (L2VA / FL2VA)')
    p.add_argument('--no-think', action='store_true', help='Skip model reasoning (faster, usually weaker)')
    p.add_argument('--temperature', type=float, default=0.7)
    p.add_argument('--max-side', type=int, default=1024, help='Downscale images to this long side')
    p.add_argument('--url', help='Chat completions URL (default: first running backend)')
    p.add_argument('--model', help='Served model name (with --url)')
    p.add_argument('--out', type=Path, help='Write the prompt here (default: out/<timestamp>.txt)')
    a = p.parse_args()

    start = time.time()
    try:
        mode = select_mode(a.ref, a.first, a.last)
        if not a.url:
            a.url, a.model = backend()
        print(f'director: {mode}, {a.seconds:g}s -> {a.model} ...', file=sys.stderr)
        mode, text, tokens, model = direct(a.idea, a.seconds, a.ref, a.first, a.last, not a.no_think,
                                    a.temperature, a.max_side, a.url, a.model)
    except DirectorError as e:
        sys.exit(f'director: {e}')
    out = save(text, a.out)
    print(text)
    print(f'\ndirector: saved {out}  ({model}, {time.time() - start:.0f}s, {tokens or "?"} tokens)', file=sys.stderr)


if __name__ == '__main__':
    main()
