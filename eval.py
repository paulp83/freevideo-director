#!/usr/bin/env python3
"""Director model A/B: run fixed cases through director.direct and score H3 format + grounding.

  python3 eval.py --label qwen36-35b --url http://127.0.0.1:8078/v1/chat/completions --model qwen3.6-35b-a3b
  python3 eval.py --label qwen38-27b --url http://127.0.0.1:8144/v1/chat/completions --model qwen3.8-27b
  python3 eval.py --portable --label qwen38-27b ...        # only cases with no private photos
  python3 eval.py --resume --label qwen38-27b ...          # continue an interrupted label
Cases live in eval/cases.json (data, not code); images are fixture ids resolved under
eval/fixtures/ (docs/evaluation.md). Writes eval/<label>/<case>-<run>.txt plus
eval/<label>/results.json: schema 2 - a run header recording model/call/skill/validator
versions, then one row per attempted (case, run). results.json is rewritten atomically after
every call, so a crash or ctrl-C keeps everything that already ran; failed calls are rows too
(status 'failed_call'), never missing data. Non-zero exit when any call failed.
"""
import argparse, hashlib, json, os, platform, re, shutil, socket, subprocess, sys, time
import urllib.request
from pathlib import Path

import config
import director, h3check

HERE = Path(__file__).resolve().parent
CASES_FILE = HERE / 'eval' / 'cases.json'
FIXTURES_DEFAULT = HERE / 'eval' / 'fixtures'
MAKE_FIXTURES = HERE / 'eval' / 'make_fixtures.py'
SCHEMA_VERSION = 2


def sha256_file(path, width=16):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:width]


def _utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def load_cases(path=CASES_FILE):
    data = json.loads(Path(path).read_text())
    return data['cases'], data.get('fixtures', {})


def case_fixture_ids(case):
    """The fixture ids a case needs, in picture order."""
    return list(case.get('refs', [])) + [x for x in (case.get('first'), case.get('last')) if x]


def n_images(case):
    return len(case_fixture_ids(case))


def case_is_portable(case, fixtures):
    """True when no fixture of the case is a private photo (runnable with --portable)."""
    return all(fixtures[f]['kind'] != 'private' for f in case_fixture_ids(case))


def fixture_search(fid, fixtures, fixture_dir, overrides):
    """Paths tried for one fixture id, in order: exact override, fixture dir, repo default.
    Generated fixtures resolve to their slot under <fixture_dir>/generated/."""
    spec = fixtures[fid]
    if spec['kind'] == 'generated':
        return [Path(fixture_dir) / 'generated' / spec['file']]
    tried = []
    override = overrides.get(spec.get('env')) if spec.get('env') else None
    if override:
        tried.append(Path(str(override)).expanduser())
    tried.append(Path(fixture_dir) / spec['file'])
    if Path(fixture_dir) != FIXTURES_DEFAULT:
        tried.append(FIXTURES_DEFAULT / spec['file'])
    return tried


def prepare_generated(fixtures, fixture_dir, needed_ids, ffmpeg):
    """Build missing generated fixtures on demand; None on success, else the failure message."""
    gen_dir = Path(fixture_dir) / 'generated'
    todo = sorted({fixtures[f]['file'] for f in needed_ids
                   if fixtures[f]['kind'] == 'generated' and not (gen_dir / fixtures[f]['file']).is_file()})
    if not todo:
        return None
    if not shutil.which(ffmpeg):
        return f'{ffmpeg} not found on PATH; cannot build generated fixtures: ' + ', '.join(todo)
    res = subprocess.run([sys.executable, str(MAKE_FIXTURES), '--out', str(gen_dir),
                          '--only', ','.join(todo)], capture_output=True, text=True)
    return None if res.returncode == 0 else (res.stderr or res.stdout).strip()


def score(case, mode, text):
    low = text.lower()
    rep = h3check.report(mode, text, case['seconds'], n_images(case))
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


def models_at(url):
    """Model ids the backend reports at /v1/models (best effort; never fails the run)."""
    probe = url.replace('/chat/completions', '/models')
    try:
        with urllib.request.urlopen(probe, timeout=3) as r:
            return [m.get('id') for m in json.load(r).get('data', [])]
    except (OSError, ValueError) as e:
        return {'error': f'{type(e).__name__}: {e}'}


def git_state():
    try:
        run = lambda *a: subprocess.run(['git', '-C', str(HERE), *a], capture_output=True, text=True)
        return {'commit': run('rev-parse', 'HEAD').stdout.strip() or None,
                'dirty': bool(run('status', '--porcelain').stdout.strip())}
    except OSError:
        return {'commit': None, 'dirty': None}


def skill_hashes():
    return {f.relative_to(director.SKILL).as_posix(): sha256_file(f)
            for f in sorted(director.SKILL.rglob('*')) if f.is_file()}


def run_meta(label, url, model, runs, settings, cases_file):
    return {
        'schema_version': SCHEMA_VERSION,
        'label': label,
        'started': _utc(), 'finished': None,
        'director_url': url, 'configured_model': model,
        'models_endpoint': models_at(url),
        'call_settings': dict(settings, skill_files=skill_hashes()),
        'skill_revision': director.skill_revision(),
        'validator': {'h3check_version': h3check.H3CHECK_VERSION,
                      'h3check_sha256': sha256_file(HERE / 'h3check.py'),
                      'cases_sha256': sha256_file(cases_file),
                      'eval_sha256': sha256_file(HERE / 'eval.py')},
        'git': git_state(),
        'python': platform.python_version(),
        'host_hash': hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16],
        'runs_per_case': runs,
    }


def _validator_key(meta):
    """The part of the header that decides comparability (eval_sha is bookkeeping only)."""
    v = meta.get('validator') or {}
    return {k: v.get(k) for k in ('h3check_version', 'h3check_sha256', 'cases_sha256')}


def write_results(out, meta, rows):
    """Atomic rewrite of results.json after every case-run: a crash keeps what already ran."""
    tmp = out / 'results.json.tmp'
    tmp.write_text(json.dumps(dict(meta, rows=rows), indent=1) + '\n')
    os.replace(tmp, out / 'results.json')



def _ordered(plan, done, new):
    """Saved + newly-run rows in plan order; resume rows for no-longer-planned runs are kept."""
    ordered = [row for row in (done.get((c['id'], n)) or new.get((c['id'], n))
                               for c, n in plan) if row]
    planned = {(c['id'], n) for c, n in plan}
    return ordered + [r for k, r in done.items() if k not in planned]

def run_eval(label, url, model, *, runs=2, case_filter=None, portable=False, resume=False,
             out_root=None, cases_file=CASES_FILE, fixture_dir=None, overrides=None,
             ffmpeg='ffmpeg', temperature=0.7, max_tokens=12000, seed=None, top_p=None,
             timeout=600, quiet=False):
    """Run the whole plan; returns (meta, rows, exit_code). Every (case, run) gets a row."""
    out_root = Path(out_root) if out_root else HERE / 'eval'
    out = out_root / label
    out.mkdir(parents=True, exist_ok=True)
    fixture_dir = Path(fixture_dir) if fixture_dir else FIXTURES_DEFAULT
    overrides = overrides or {}
    cases, fixtures = load_cases(cases_file)

    settings = {'temperature': temperature, 'max_tokens': max_tokens, 'top_p': top_p,
                'seed': seed, 'think': True, 'timeout_s': timeout}
    meta = run_meta(label, url, model, runs, settings, cases_file)

    plan = []
    for case in cases:
        if case_filter and not any(case['id'].startswith(c) for c in case_filter.split(',')):
            continue
        if portable and not case_is_portable(case, fixtures):
            continue
        for run in range(1, runs + 1):
            plan.append((case, run))

    done = {}
    if resume:
        prev_path = out / 'results.json'
        if prev_path.is_file():
            prev = json.loads(prev_path.read_text())
            if not isinstance(prev, dict) or prev.get('schema_version') != SCHEMA_VERSION:
                print(f'{label}: --resume needs a schema {SCHEMA_VERSION} results.json '
                      f'(this one is schema {prev.get("schema_version") if isinstance(prev, dict) else 1}); '
                      're-run under a new label', file=sys.stderr)
                return meta, [], 2
            if _validator_key(prev) != _validator_key(meta):
                print(f'{label}: --resume refused - the validator/case set changed since the saved run\n'
                      f'  saved:   {json.dumps(_validator_key(prev), sort_keys=True)}\n'
                      f'  current: {json.dumps(_validator_key(meta), sort_keys=True)}\n'
                      '  start a new label (or rescore.py the old one)', file=sys.stderr)
                return meta, [], 2
            if (prev.get('director_url'), prev.get('configured_model')) != (url, model):
                print(f'{label}: --resume refused - saved run used {prev.get("director_url")} '
                      f'({prev.get("configured_model")})', file=sys.stderr)
                return meta, [], 2
            done = {(r['case'], r['run']): r for r in prev.get('rows', [])}
            meta['started'] = prev.get('started') or meta['started']

    # one generator pass for every generated fixture the plan needs
    needed = {f for case, _ in plan for f in case_fixture_ids(case)}
    gen_error = prepare_generated(fixtures, fixture_dir, needed, ffmpeg)
    new = {}
    write_results(out, meta, _ordered(plan, done, new))
    for case, run in plan:
        if (case['id'], run) in done:
            continue
        missing = None
        paths = []
        for fid in case_fixture_ids(case):
            hit = next((p for p in fixture_search(fid, fixtures, fixture_dir, overrides) if p.is_file()), None)
            if hit:
                paths.append(str(hit))
            elif missing is None:
                missing = (fid, [str(p) for p in fixture_search(fid, fixtures, fixture_dir, overrides)])
        start = time.time()
        if missing:
            row = dict(case=case['id'], run=run, status='skipped_fixture_missing', mode=None,
                       seconds=0.0, tokens=None, tok_s=None, passed=0, total=0, failed=[], error=None,
                       missing_fixture=missing[0], searched=missing[1],
                       **({'reason': gen_error} if gen_error else {}))
        else:
            try:
                refs = [paths[i] for i, _ in enumerate(case.get('refs', []))]
                extra = len(refs)
                first = paths[extra] if case.get('first') else None
                last = paths[extra + 1] if case.get('last') else None
                r = director.direct_report(case['idea'], case['seconds'], refs, first, last,
                                           temperature=temperature, url=url, model=model,
                                           timeout=timeout, seed=seed, top_p=top_p,
                                           max_tokens=max_tokens)
                secs = time.time() - start
                checks, stats = score(case, r['mode'], r['text'])
                (out / f"{case['id']}-{run}.txt").write_text(r['text'] + '\n')
                row = dict(case=case['id'], run=run, status='ok', mode=r['mode'],
                           seconds=round(secs, 1), tokens=r['tokens'],
                           tok_s=round(r['tokens'] / secs, 1) if r['tokens'] else None,
                           passed=sum(checks.values()), total=len(checks),
                           failed=[k for k, v in checks.items() if not v], error=None,
                           attempts=r['calls'], retried=r['retried'],
                           first_failures=r['first_failures'], **stats)
            except Exception as e:                              # any model-call failure becomes a row
                row = dict(case=case['id'], run=run, status='failed_call', mode=None,
                           seconds=round(time.time() - start, 1), tokens=None, tok_s=None,
                           passed=0, total=0, failed=[], error=str(e),
                           error_class=type(e).__name__, attempts=getattr(e, 'calls', None))
        new[(case['id'], run)] = row
        meta['finished'] = _utc()
        write_results(out, meta, _ordered(plan, done, new))
        if not quiet:
            r = row
            tail = ('FAIL ' + ','.join(r['failed'])) if r['failed'] else ''
            tail += f" {r['status']}: {r.get('error') or r.get('missing_fixture') or ''}".rstrip() \
                if r['status'] != 'ok' else ''
            print(f"{r['case']:<22} run{run} {r['seconds']:>6}s {str(r['tokens']):>6} tok  "
                  f"{r['passed']}/{r['total']}  {tail}", flush=True)
    meta['finished'] = _utc()
    all_rows = _ordered(plan, done, new)
    write_results(out, meta, all_rows)

    planned = len(plan)
    skipped = sum(r['status'] == 'skipped_fixture_missing' for r in all_rows)
    failed_calls = sum(r['status'] == 'failed_call' for r in all_rows)
    passed = sum(r['status'] == 'ok' and not r['failed'] for r in all_rows)
    checks_ok = sum(r['passed'] for r in all_rows)
    checks_tot = sum(r['total'] for r in all_rows)
    lat = sorted(r['seconds'] for r in all_rows if r['status'] != 'skipped_fixture_missing')
    if not quiet:
        print(f"\n{label}: {passed}/{planned} passed, {skipped} skipped, {failed_calls} failed calls  "
              f"(checks {checks_ok}/{checks_tot}, median latency {lat[len(lat) // 2] if lat else 0}s, "
              f"total tokens {sum(r['tokens'] or 0 for r in all_rows)})")
    return meta, all_rows, (1 if failed_calls else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--label', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--runs', type=int, default=2)
    p.add_argument('--cases', help='comma list of case-id prefixes')
    p.add_argument('--portable', action='store_true',
                   help='only cases whose images are generated fixtures (no private photos)')
    p.add_argument('--resume', action='store_true', help='continue an interrupted label')
    p.add_argument('--dir', help='label-output root (default eval/)')
    p.add_argument('--temperature', type=float, default=0.7)
    p.add_argument('--max-tokens', type=int, default=12000)
    p.add_argument('--seed', type=int, help='send a fixed sampling seed')
    p.add_argument('--top-p', type=float, help='send a fixed top_p')
    p.add_argument('--timeout', type=float, default=600, help='per-call HTTP timeout seconds')
    config.add_service_args(p, ('eval_fixture_dir', 'eval_group_image', 'eval_range_image'))
    a = p.parse_args()
    _, _, code = run_eval(a.label, a.url, a.model, runs=a.runs, case_filter=a.cases,
                          portable=a.portable, resume=a.resume, out_root=a.dir,
                          fixture_dir=config.value('eval_fixture_dir'),
                          overrides={'EVAL_GROUP_IMAGE': config.value('eval_group_image'),
                                     'EVAL_RANGE_IMAGE': config.value('eval_range_image')},
                          ffmpeg=config.value('ffmpeg'), temperature=a.temperature,
                          max_tokens=a.max_tokens, seed=a.seed, top_p=a.top_p, timeout=a.timeout)
    sys.exit(code)


if __name__ == '__main__':
    main()
