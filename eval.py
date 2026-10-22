#!/usr/bin/env python3
"""Director model A/B: run fixed cases through director.direct and score H3 format + grounding.

  python3 eval.py --label qwen36-35b --url http://127.0.0.1:8078/v1/chat/completions --model qwen3.6-35b-a3b
  python3 eval.py --label qwen38-27b --url http://127.0.0.1:8144/v1/chat/completions --model qwen3.8-27b
Writes eval/<label>/<case>-<run>.txt and eval/<label>/results.json.
"""
import argparse, json, re, time
from pathlib import Path

import config
import director, h3check

HERE = Path(__file__).resolve().parent
GROUP = str(Path('~/Pictures/group.jpg').expanduser())
RANGE = str(config.value('comfy_root') / 'input/freevideo/a2fcc1dac3784520adc9e4880939acaf/rangels.jpg')

CASES = [
    dict(id='C1-t2va-boat', seconds=6, idea='a paper boat drifts down a rain gutter into a puddle',
         grounding=[]),
    dict(id='C2-t2va-dialogue', seconds=10,
         idea="a street food chef in Bangkok flips noodles in a flaming wok and tells the camera "
              "'This is how my grandmother did it.'",
         grounding=['grandmother'], dialogue=True),
    dict(id='C3-ref-lightsaber', seconds=10, refs=[GROUP],
         idea='the two people on the right side of the photo have a Jedi lightsaber fight in this parking lot',
         grounding=['team pizza', 'red', 'cap|hat']),
    dict(id='C4-i2va-cheer', seconds=5, first=GROUP,
         idea='everyone cheers and the man on the far right jumps for joy',
         grounding=['purple|team pizza']),
    dict(id='C5-ref2-ranch', seconds=12, refs=[GROUP, RANGE],
         idea='the Team Pizza crew from the first photo rides horses into the ranch from the second photo at '
              'sunset, and the wranglers lasso the man in the cap',
         grounding=['reston wranglers', 'team pizza', 'lasso|rope'], labels=2),
    dict(id='C6-ref-spin', seconds=8, refs=[RANGE],
         idea='the three women let go of the rope and the man in the middle spins free, then tips his hat',
         grounding=['reston wranglers', 'plaid|flannel', 'bandana', 'rope'], labels=1),
]



def score(case, mode, text):
    low = text.lower()
    n_images = len(case.get('refs', [])) or len([x for x in (case.get('first'), case.get('last')) if x])
    rep = h3check.report(mode, text, case['seconds'], n_images)
    checks = {r['check']: r['ok'] for r in rep}
    stamps = [int(m) * 60 + float(s) for m, s in re.findall(r'At (\d\d):(\d\d(?:\.\d+)?)', text)]
    shots = re.findall(r'\[Shot (\d+)\]', text)
    labels = {int(n) for n in re.findall(r'<Picture (\d+)>', text)}
    if case.get('labels'):
        checks['all_images_used'] = labels == set(range(1, case['labels'] + 1))
    if case.get('dialogue'):
        checks['dialogue_tag'] = bool(re.search(r'<d>.*grandmother.*</d>', text, re.I | re.S))
    for g in case['grounding']:
        checks['mentions:' + g] = bool(re.search(g, low))
    return checks, {'shots': len(set(shots)), 'last_cut': max(stamps, default=0), 'chars': len(text)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--label', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--runs', type=int, default=2)
    p.add_argument('--cases', help='comma list of case-id prefixes')
    a = p.parse_args()
    out = HERE / 'eval' / a.label
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in CASES:
        if a.cases and not any(case['id'].startswith(c) for c in a.cases.split(',')):
            continue
        for run in range(1, a.runs + 1):
            start = time.time()
            try:
                mode, text, tokens, _ = director.direct(case['idea'], case['seconds'], case.get('refs', []),
                                                     case.get('first'), case.get('last'), True,
                                                     url=a.url, model=a.model)
                error = None
            except director.DirectorError as e:
                mode, text, tokens, error = None, "", None, str(e)
            secs = time.time() - start
            (out / f"{case['id']}-{run}.txt").write_text(text + '\n')
            checks, stats = score(case, mode, text) if text else ({}, {})
            passed = sum(checks.values())
            rows.append(dict(case=case['id'], run=run, mode=mode, seconds=round(secs, 1), tokens=tokens,
                             tok_s=round(tokens / secs, 1) if tokens else None, passed=passed,
                             total=len(checks), failed=[k for k, v in checks.items() if not v], error=error,
                             **stats))
            r = rows[-1]
            print(f"{r['case']:<20} run{run} {r['seconds']:>6}s {str(tokens):>6} tok  {passed}/{len(checks)}"
                  f"  {('FAIL ' + ','.join(r['failed'])) if r['failed'] else ''}{error or ''}", flush=True)
    (out / 'results.json').write_text(json.dumps(rows, indent=1))
    ok = sum(r['passed'] for r in rows); tot = sum(r['total'] for r in rows)
    t = [r['seconds'] for r in rows]
    print(f"\n{a.label}: checks {ok}/{tot}  median latency {sorted(t)[len(t) // 2]}s  "
          f"total tokens {sum(r['tokens'] or 0 for r in rows)}")


if __name__ == '__main__':
    main()
