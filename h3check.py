#!/usr/bin/env python3
"""Generic H3 output-format checks shared by eval.py, rescore.py and the director retry loop.

Every check encodes a rule the pinned skill in skill/ states; docs/prompt-validation.md maps
each check name to the skill file and line, and lists its stable one-line message. `failures()`
returns the names of the checks a reply fails (empty list = all pass); `report()` returns one
{check, ok, detail} entry per applicable check, the `detail` being a one-line human message
used for retry feedback and diagnostics.

Conventions: a *section* is a top-level `label:` block ending at the next known label line;
a *cut stamp* is an `At MM:SS[.mmm]` marker that opens a shot after the first (the skill never
puts a stamp on [Shot 1] - the timeline starts at 00:00).
"""
import re

BASE = ['integrated_multimodal_description', 'overall_soundscape', 'non_diegetic_music']
REF = ['subject_definitions', 'summary', 'retention_analysis', 'detailed_description',
       'overall_soundscape', 'non_diegetic_music']
KNOWN_SECTIONS = set(BASE) | set(REF)
I2VA_LINE = 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'
ALIGN = 'How the reference pictures align with the target video — '
TASK_TYPES = ('keyframe completion', 'reference generation', 'video editing',
              'video continuation', 'audio reuse', 'audio reference')

# One-line default messages per check (docs/prompt-validation.md keeps this list in sync).
MESSAGES = {
    'empty_reply': 'the reply is empty - no prompt text at all',
    'refusal_only': 'the reply is only a refusal or apology, not a prompt',
    'code_fence_only': 'the reply is only a markdown code fence with no prompt inside',
    'fields_present': 'a required section is missing',
    'fields_once': 'a section appears more than once',
    'fields_in_order': 'the sections are not in the skill order',
    'fields_non_empty': 'a section has no content after its label',
    'unknown_section': 'a section label does not exist in this mode',
    'no_instruction_line': 'this mode has no image-alignment instruction; the reply must open with a section label',
    'instruction_line': 'the first line is not the exact keyframe-alignment instruction for this mode and duration',
    'picture1_in_shot1': '[Shot 1] never references <Picture 1>, the first frame',
    'shot_numbering': 'shot labels must run [Shot 1], [Shot 2], ... in order with no gaps or duplicates',
    'first_shot_zero': '[Shot 1] must not carry a timestamp; the timeline starts at 00:00',
    'shot_cuts': 'every shot after the first opens with a cut time directly after its [Shot N] label',
    'timestamps_format': 'cut times must be written At MM:SS or At MM:SS.mmm',
    'timestamps_monotonic': 'cut times must strictly increase and come after 00:00',
    'timestamps_in_range': 'a cut time falls at or past the requested duration',
    'labels_valid': '<Picture N> labels do not match the images that were provided',
    'picture_labels_defined': 'a <Picture N> label is used without being defined in subject_definitions',
    'summary_task_prefix': 'summary must open with a [task type] prefix',
    'dialogue_tags': 'every <d>...</d> dialogue block must be balanced and non-empty',
    'no_markdown': 'the reply contains markdown fences, headings or bold',
    'no_placeholders': 'the reply contains a leftover template placeholder or unknown <...> tag',
}

_LOOSE_AT = re.compile(r'At[ \t]+\d{1,3}:\d+(?:\.\d+)?')
_STRICT_AT = re.compile(r'At \d{2}:\d{2}(?:\.\d{1,3})?\Z')
_CUT_AFTER = re.compile(r'\s*(At[ \t]+\d{1,3}:\d+(?:\.\d+)?)')
_SHOT = re.compile(r'\[Shot (\d+)\]')
_SECTION = re.compile(r'(?m)^([a-z][a-z0-9_]*):')
_REFUSAL = re.compile(r"(?i)^(i'?m sorry|i am sorry|sorry[,!] but|i (?:can'?t|cannot|won'?t|will not)"
                      r"|i'?m unable|i am unable|as an (?:ai|language model))\b")
_PICT = re.compile(r'<Picture (\d+)>')
_PICT_BARE = re.compile(r'(?<![<\w])Picture (\d+)')
_PLACEHOLDER_TAG = re.compile(r'<([A-Za-z][A-Za-z ]*\d*)>')


def _at_value(raw):
    mm, _, rest = re.sub(r'^At\s+', '', raw).partition(':')
    return int(mm) * 60 + float(rest)


def applicable(mode):
    """The check names a reply in this mode is scored on, in report() order."""
    names = ['empty_reply', 'code_fence_only', 'refusal_only',
             'fields_present', 'fields_once', 'fields_in_order', 'fields_non_empty',
             'unknown_section']
    names += ['no_instruction_line'] if mode in ('t2va', 'ref2va') else ['instruction_line']
    names += ['shot_numbering', 'first_shot_zero', 'shot_cuts',
              'timestamps_format', 'timestamps_monotonic', 'timestamps_in_range', 'labels_valid']
    if mode == 'i2va':
        names += ['picture1_in_shot1']
    if mode == 'ref2va':
        names += ['picture_labels_defined', 'summary_task_prefix']
    names += ['dialogue_tags', 'no_markdown', 'no_placeholders']
    return names


def _sections(text, fields):
    """label -> content, plus the top-level section label lines in document order."""
    hits = [(m.start(), m.group(1)) for m in _SECTION.finditer(text) if m.group(1) in KNOWN_SECTIONS]
    content = {}
    for i, (pos, label) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        body = text[pos + len(label) + 1:end].strip()
        if label not in content:
            content[label] = body
    seen, dupes = {}, []
    for _, label in hits:
        seen[label] = seen.get(label, 0) + 1
    for label, n in seen.items():
        if n > 1:
            dupes.append(label)
    return content, [label for _, label in hits], dupes


def report(mode, text, seconds, n_images):
    """One {check, ok, detail} per applicable check; detail is a one-line message on failure."""
    fields = REF if mode == 'ref2va' else BASE
    out = []

    def chk(name, ok, detail=''):
        out.append({'check': name, 'ok': bool(ok), 'detail': '' if ok else (detail or MESSAGES[name])})

    if not isinstance(text, str):
        text = ''
    raw = text.strip()
    chk('empty_reply', bool(raw))
    stripped = re.sub(r'^```[^\n]*\n?|\n?\s*```$', '', raw).strip() if '```' in raw else raw
    chk('code_fence_only', not ('```' in raw and not stripped))

    content, order, dupes = _sections(stripped, fields)
    missing = [f for f in fields if f not in content]
    chk('refusal_only', not (_REFUSAL.match(stripped) and missing and len(stripped) < 600))
    chk('fields_present', not missing, 'missing section(s): ' + ', '.join(missing))
    chk('fields_once', not dupes, 'appears twice: ' + ', '.join(dupes))
    present = [f for f in fields if f in content]
    seq = [label for label in dict.fromkeys(order) if label in present]
    chk('fields_in_order', seq == present,
        'sections appear as ' + ', '.join(seq) + '; must be ' + ', '.join(present))
    empty_secs = [f for f in fields if f in content and not content[f]]
    chk('fields_non_empty', not empty_secs, 'no content after label: ' + ', '.join(empty_secs))
    unknown = sorted({m.group(1) for m in _SECTION.finditer(stripped) if m.group(1) not in fields})
    chk('unknown_section', not unknown, 'not a section in this mode: ' + ', '.join(unknown))

    desc_field = 'detailed_description' if mode == 'ref2va' else 'integrated_multimodal_description'
    first_line = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), '')
    if mode in ('t2va', 'ref2va'):
        want = desc_field if mode == 't2va' else 'subject_definitions'
        chk('no_instruction_line', first_line.startswith(want + ':'),
            f'{mode} has no alignment instruction; the reply must open with "{want}:"')
    else:
        detail = MESSAGES['instruction_line']
        shots = _SHOT.findall(content.get(desc_field, ''))
        if mode == 'i2va':
            ok = first_line == I2VA_LINE
        else:
            if mode == 'l2va':
                pat = (re.escape(ALIGN) + r'<Picture 1> \(from \[Shot (\d+)\]\) aligns with the '
                       r'(\d+\.\d{2})-second mark of the target video\.')
            else:  # fl2va - the skill writes this template without angle brackets
                pat = (re.escape(ALIGN) + r'Picture 1 \(from Shot 1\) aligns with the 0\.00-second mark '
                       r'of the target video; Picture 2 \(from Shot (\d+)\) aligns with the '
                       r'(\d+\.\d{2})-second mark of the target video\.')
            m = re.fullmatch(pat, first_line)
            ok = bool(m) and m.group(2) == f'{seconds:.2f}' and (not shots or int(m.group(1)) == int(shots[-1]))
            if m and not ok:
                detail = f'the instruction must name Shot {len(shots)} and {seconds:.2f} seconds'
        chk('instruction_line', ok, detail)

    body = content.get(desc_field, '')
    labels = list(_SHOT.finditer(body))
    nums = [int(m.group(1)) for m in labels]
    if not nums:
        chk('shot_numbering', False, 'no [Shot N] labels found')
    elif nums != list(range(1, len(nums) + 1)):
        chk('shot_numbering', False, 'expected [Shot 1]..[Shot %d] in order, found [%s]'
            % (len(nums), '], ['.join(str(n) for n in nums)))
    else:
        chk('shot_numbering', True)

    stamps = [(m.start(), m.group(0)) for m in _LOOSE_AT.finditer(body)]
    shot1_end = labels[1].start() if len(labels) > 1 else len(body) if labels else 0
    chk('first_shot_zero', not any(pos < shot1_end for pos, _ in stamps),
        'a cut time appears inside [Shot 1], which starts at 00:00 without one')

    cuts, cut_pos, missing_cut = [], set(), []
    for i, m in enumerate(labels):
        if int(m.group(1)) == 1:
            continue
        g = _CUT_AFTER.match(body, m.end())
        if not g:
            missing_cut.append(m.group(0))
            continue
        raw = g.group(1)
        cut_pos.add(body.find(raw, m.end(), m.end() + 60))
        if _STRICT_AT.fullmatch(raw):
            cuts.append(_at_value(raw))
    strays = [raw for pos, raw in stamps if pos not in cut_pos and pos >= shot1_end]
    if missing_cut or strays:
        chk('shot_cuts', False, 'shot(s) without a cut time right after the label: '
            + ', '.join(missing_cut) + ('; stray cut time: ' + ', '.join(strays) if strays else ''))
    else:
        chk('shot_cuts', True)

    malformed = [raw for _, raw in stamps if not _STRICT_AT.fullmatch(raw)]
    chk('timestamps_format', not malformed, 'malformed stamp: ' + ', '.join(malformed))
    monotonic = all(b > a for a, b in zip(cuts, cuts[1:])) and (not cuts or cuts[0] > 0)
    chk('timestamps_monotonic', monotonic,
        'cut times must strictly increase after 00:00: ' + ', '.join(f'{c:g}' for c in cuts))
    out_of_range = [c for c in cuts if not 0 <= c < seconds]
    chk('timestamps_in_range', not out_of_range,
        'cut time(s) outside 0..%gs: ' % seconds + ', '.join(f'{c:g}' for c in out_of_range))

    used = {int(n) for n in _PICT.findall(stripped)}
    if mode == 'fl2va':
        used |= {int(n) for n in _PICT_BARE.findall(stripped)}
    want = set(range(1, n_images + 1))
    if n_images == 0:
        chk('labels_valid', not used, 'this mode has no images; do not reference '
            + ', '.join(f'<Picture {n}>' for n in sorted(used)))
    else:
        bad_bits = []
        if used - want:
            bad_bits.append('references ' + ', '.join(f'<Picture {n}>' for n in sorted(used - want))
                            + f' but only {n_images} image(s) were provided')
        if want - used:
            bad_bits.append('never references ' + ', '.join(f'<Picture {n}>' for n in sorted(want - used)))
        chk('labels_valid', not bad_bits, '; '.join(bad_bits))

    if mode == 'i2va':
        block = body[labels[0].end():shot1_end] if labels else body
        chk('picture1_in_shot1', '<Picture 1>' in block)
    if mode == 'ref2va':
        defined = {int(n) for n in _PICT.findall(content.get('subject_definitions', ''))}
        undef = sorted(used - defined)
        chk('picture_labels_defined', not undef, 'undefined: '
            + ', '.join(f'<Picture {n}>' for n in undef))
        summary = content.get('summary', '')
        m = re.match(r'\[([^\]]+)\]', summary)
        types = m.group(1).split(' + ') if m else None
        ok = bool(types) and all(t.strip() in TASK_TYPES for t in types) and len(types) == len(set(types))
        chk('summary_task_prefix', ok, 'summary opens '
            + (repr(summary[:40]) + '; expected e.g. "[reference generation] ..."' if m or summary else 'empty'))
    blocks = [(m.start(), m.end()) for m in re.finditer(r'<(/?)d>', stripped)]
    problem = ''
    depth, open_at = 0, None
    for pos, end in blocks:
        closing = stripped[pos + 1] == '/'
        if closing:
            if depth == 0:
                problem = 'a </d> closes no <d>'
                break
            if not stripped[open_at:pos].strip():
                problem = 'an empty <d>...</d> block'
                break
            depth -= 1
        else:
            if depth:
                problem = 'a <d> opens inside another <d>'
                break
            depth, open_at = 1, end
    if not problem and depth:
        problem = 'an unclosed <d> block'
    chk('dialogue_tags', not problem, problem)

    md_bits = [b for b, hit in (('code fence', '```' in stripped), ('bold **', '**' in stripped),
                                ('heading', re.search(r'(?m)^\s*#', stripped))) if hit]
    chk('no_markdown', not md_bits, 'contains ' + ', '.join(md_bits))

    allowed = {'d', 'scenetrans', 'cutoff'}
    allowed |= {'Picture', 'Subject', 'Video', 'Audio'} if mode == 'ref2va' else {'Picture'}
    bad_tags = []
    for token in _PLACEHOLDER_TAG.findall(stripped):
        word = token.split()[0] if token.split() else token
        if word not in allowed:
            bad_tags.append('<' + token + '>')
    literals = [lit for lit in ('<...', 'TODO', '[insert', '[INSERT') if lit in stripped]
    chk('no_placeholders', not bad_tags and not literals,
        'leftover: ' + ', '.join(bad_tags + literals))
    return out


def failures(mode, text, seconds, n_images):
    """Names of the generic format checks this text fails; empty list means all pass."""
    return [r['check'] for r in report(mode, text, seconds, n_images) if not r['ok']]
