#!/usr/bin/env python3
"""Re-score saved eval outputs with the current h3check, without re-running any model.

  python3 rescore.py            # every eval/<label> with a results.json

Handles both results.json shapes: the old flat list (schema 1: validator was never recorded)
and the schema 2 dict with a run header. Only rows that produced a reply are re-scored
(status 'ok' / error-less); failed calls and skipped fixtures keep their recorded shape.
Writes eval/<label>/rescored.json: per row the failed checks recorded at run time beside the
checks from today's scorer, and a validator block {recorded, current} so a version change is
printed, never hidden (see eval_compare.py, which refuses to compare across validators).
Case durations, image counts and grounding come from eval/cases.json, never from latency."""
import json
from pathlib import Path

import h3check
from eval import CASES_FILE, load_cases, score, sha256_file

HERE = Path(__file__).resolve().parent


def current_validator(cases_file=CASES_FILE):
    return {'h3check_version': h3check.H3CHECK_VERSION,
            'h3check_sha256': sha256_file(HERE / 'h3check.py'),
            'cases_sha256': sha256_file(cases_file)}


def score_label(label_dir, cases_file=CASES_FILE):
    doc = json.loads((label_dir / 'results.json').read_text())
    meta, rows = (doc, doc.get('rows', [])) if isinstance(doc, dict) else ({}, doc)
    schema = doc.get('schema_version', 1) if isinstance(doc, dict) else 1
    cases, _ = load_cases(cases_file)
    byid = {c['id']: c for c in cases}
    out, old_pass = [], 0
    for row in rows:
        status = row.get('status') or ('ok' if row.get('error') is None else 'failed_call')
        old_failed = list(row.get('failed') or [])
        old_pass += int(status == 'ok' and not old_failed)
        rec = dict(case=row['case'], run=row['run'], mode=row.get('mode'), status=status,
                   old_passed=row.get('passed'), old_total=row.get('total'), old_failed=old_failed)
        text = label_dir / f"{row['case']}-{row['run']}.txt"
        case = byid.get(row['case'])
        if status == 'ok' and case and text.is_file():
            checks, _ = score(case, row['mode'], text.read_text())
            new_failed = [k for k, v in checks.items() if not v]
            rec.update(new_passed=sum(checks.values()), new_total=len(checks), new_failed=new_failed,
                       newly_failed=[c for c in new_failed if c not in old_failed])
        else:
            note = ('no reply to re-score' if status == 'ok'
                    else 'kept as recorded' if status == 'failed_call' else 'fixture missing')
            rec.update(new_passed=None, new_total=None, new_failed=None, newly_failed=[], note=note)
        out.append(rec)
    new_clean = sum(1 for r in out if r['new_failed'] is not None and not r['new_failed'])
    recorded = meta.get('validator') or {'h3check_version': None,
                                         'note': f'schema {schema}: validator not recorded at run time'}
    current = current_validator(cases_file)
    summary = dict(rows=len(out), old_clean=old_pass, new_clean=new_clean,
                   failed_calls=sum(1 for r in out if r['status'] == 'failed_call'),
                   skipped=sum(1 for r in out if r['status'] == 'skipped_fixture_missing'),
                   unknown=sum(1 for r in out if r['new_failed'] is None),
                   schema=schema,
                   validator={'recorded': recorded, 'current': current},
                   validator_changed=bool(recorded.get('h3check_version') is not None
                                          and recorded != current),
                   flipped=[f"{r['case']}-{r['run']}: +{', '.join(r['newly_failed'])}"
                            for r in out if r['newly_failed']])
    (label_dir / 'rescored.json').write_text(json.dumps(dict(summary=summary, rows=out), indent=2) + '\n')
    return summary


def main():
    for label_dir in sorted(p for p in (HERE / 'eval').iterdir()
                            if (p / 'results.json').exists()):
        s = score_label(label_dir)
        v = s['validator']
        print(f"{label_dir.name}: rows={s['rows']} (schema {s['schema']}) clean old={s['old_clean']} "
              f"new={s['new_clean']} failed_calls={s['failed_calls']} skipped={s['skipped']} "
              f"unknown={s['unknown']}")
        print(f"  validator recorded: {json.dumps(v['recorded'], sort_keys=True)}")
        print(f"  validator current:  {json.dumps(v['current'], sort_keys=True)}")
        if s['validator_changed']:
            print('  WARNING: h3check/cases changed since this run; the new counts are NOT directly '
                  'comparable with other labels until eval_compare.py agrees on the validator')
        for line in s['flipped']:
            print('  ' + line)


if __name__ == '__main__':
    main()
