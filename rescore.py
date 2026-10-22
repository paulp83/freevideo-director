#!/usr/bin/env python3
"""Re-score the saved eval outputs with the current h3check, without re-running any model.

  python3 rescore.py            # every eval/<label> with a results.json

Reads eval/<label>/results.json plus the <case>-<run>.txt replies, scores each reply with
today's h3check.report, and writes eval/<label>/rescored.json: per row the old failed checks
(stored in results.json when the run happened) beside the new ones, plus a summary. Case
durations and image counts come from eval.py's CASES, never from the latency column."""
import json
from pathlib import Path

import h3check
from eval import CASES

HERE = Path(__file__).resolve().parent
META = {c['id']: dict(seconds=c['seconds'],
                      n_images=len(c.get('refs', [])) or int('first' in c))
        for c in CASES}


def score_label(label_dir):
    rows = json.loads((label_dir / 'results.json').read_text())
    out, old_pass = [], 0
    for row in rows:
        case, run, mode = row['case'], row['run'], row['mode']
        text = (label_dir / f'{case}-{run}.txt').read_text()
        rep = h3check.report(mode, text, META[case]['seconds'], META[case]['n_images'])
        new_failed = [r['check'] for r in rep if not r['ok']]
        old_failed = list(row.get('failed') or [])
        old_pass += int(row.get('error') is None and not old_failed)
        out.append(dict(case=case, run=run, mode=mode,
                        old_passed=row.get('passed'), old_total=row.get('total'),
                        old_failed=old_failed,
                        new_passed=sum(r['ok'] for r in rep), new_total=len(rep),
                        new_failed=new_failed,
                        newly_failed=[c for c in new_failed if c not in old_failed],
                        error=row.get('error')))
    new_pass = sum(1 for r in out if not r['new_failed'] and r['error'] is None)
    summary = dict(rows=len(out), old_clean=old_pass, new_clean=new_pass,
                   flipped=[f"{r['case']}-{r['run']}: +{', '.join(r['newly_failed'])}"
                            for r in out if r['newly_failed']])
    (label_dir / 'rescored.json').write_text(json.dumps(
        dict(summary=summary, rows=out), indent=2) + '\n')
    return summary


def main():
    for label_dir in sorted(p for p in (HERE / 'eval').iterdir()
                            if (p / 'results.json').exists()):
        s = score_label(label_dir)
        print(f"{label_dir.name}: rows={s['rows']} clean old={s['old_clean']} "
              f"new={s['new_clean']}")
        for line in s['flipped']:
            print('  ' + line)


if __name__ == '__main__':
    main()
