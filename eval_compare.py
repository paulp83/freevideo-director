#!/usr/bin/env python3
"""Compare saved eval runs side by side, without touching a model.

  python3 eval_compare.py qwen36-35b qwen38-27b
  python3 eval_compare.py oldlabel newlabel --dir eval --force

Reads eval/<label>/results.json; if eval/<label>/rescored.json exists its re-scored checks are
used instead, so schema 1 runs become comparable after running rescore.py first. Prints every
(case, run) where the failed checks or the call status differ between the labels, then per label
`passed/total case-runs, failed calls, skipped` - a pass count is never shown without its
denominator or the failed calls that hide inside it. Comparison is refused (exit 2) unless the
labels share the validator (h3check version + hash) and the case-set hash, because a "regression"
may just be a stricter checker; --force compares anyway with a loud warning.
"""
import argparse, json, sys
from pathlib import Path

from eval import HERE, sha256_file

VALIDATOR_KEYS = ('h3check_version', 'h3check_sha256', 'cases_sha256')


def load_label(label_dir):
    """-> dict(label, validator, checks={(case,run): (status, failed-sorted)}, rows)."""
    doc = json.loads((label_dir / 'results.json').read_text())
    meta, rows = (doc, doc.get('rows', [])) if isinstance(doc, dict) else ({}, doc)
    validator = dict((meta.get('validator') or {}))
    src = 'results.json'
    rs_path = label_dir / 'rescored.json'
    if rs_path.is_file():
        rs = json.loads(rs_path.read_text())
        by_key = {(r['case'], r['run']): r for r in rs['rows']}
        for row in rows:
            r = by_key.get((row['case'], row['run']))
            if r and r.get('new_failed') is not None:
                row['failed'], row['passed'], row['total'] = r['new_failed'], r['new_passed'], r['new_total']
        validator = dict(rs['summary']['validator']['current'])
        src = 'rescored.json'
    if validator.get('h3check_version') is None:
        validator.setdefault('note', 'unknown (schema 1 run without a recorded validator)')
    for row in rows:
        row.setdefault('status', 'ok' if row.get('error') is None else 'failed_call')
    checks = {}
    for row in rows:
        status = row['status']
        checks[(row['case'], row['run'])] = (status, tuple(sorted(row.get('failed') or [])))
    return dict(label=label_dir.name, validator=validator, src=src, rows=rows, checks=checks)


def counts(entry):
    total = len(entry['rows'])
    failed_calls = sum(1 for r in entry['rows'] if r['status'] == 'failed_call')
    skipped = sum(1 for r in entry['rows'] if r['status'] == 'skipped_fixture_missing')
    passed = sum(1 for r in entry['rows'] if r['status'] == 'ok' and not (r.get('failed') or []))
    return passed, total, failed_calls, skipped


def compare(label_dirs, force=False, stream=sys.stdout):
    entries = [load_label(d) for d in label_dirs]
    print_ = lambda *a: print(*a, file=stream)
    for e in entries:
        print_(f"{e['label']}: validator {json.dumps({k: e['validator'].get(k) for k in VALIDATOR_KEYS})}"
               + (f" note={e['validator']['note']}" if 'note' in e['validator'] else '')
               + f" (checks from {e['src']})")
    keys = {tuple(e['validator'].get(k) for k in VALIDATOR_KEYS) for e in entries}
    if len(keys) > 1:
        if not force:
            print('refusing: validators/case sets differ between labels - run rescore.py to re-score '
                  'old runs with today\'s h3check, re-run the model under the new case set, or pass '
                  '--force to compare anyway', file=sys.stderr)
            return 2
        print('WARNING: --force: comparing across different validator/case-set versions; a check '
              'difference may be the checker, not the model', file=sys.stderr)
    all_keys = sorted(set().union(*[set(e['checks']) for e in entries]))
    diffs = 0
    for key in all_keys:
        states = [(e['label'], e['checks'].get(key)) for e in entries]
        seen = {s for _, s in states if s is not None}
        if len(seen) > 1 or any(s is None for _, s in states):
            diffs += 1
            parts = []
            for label, s in states:
                if s is None:
                    parts.append(f'{label}: NO-ROW')
                elif s[0] != 'ok':
                    parts.append(f'{label}: {s[0].upper()}')
                elif s[1]:
                    parts.append(f'{label}: FAIL {",".join(s[1])}')
                else:
                    parts.append(f'{label}: PASS')
            print_(f'{key[0]} run{key[1]}: ' + '  |  '.join(parts))
    print_(f'\n{diffs} differing case-run(s)')
    for e in entries:
        passed, total, failed_calls, skipped = counts(e)
        print_(f"{e['label']}: {passed}/{total} case-runs passed, {failed_calls} failed calls, "
               f"{skipped} skipped")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('labels', nargs='+')
    p.add_argument('--dir', default=str(HERE / 'eval'), help='root holding the label dirs')
    p.add_argument('--force', action='store_true', help='compare despite validator/case differences')
    a = p.parse_args()
    dirs = []
    for name in a.labels:
        d = Path(a.dir) / name
        if not (d / 'results.json').is_file():
            p.error(f'{d}/results.json not found')
        dirs.append(d)
    sys.exit(compare(dirs, force=a.force))


if __name__ == '__main__':
    main()
