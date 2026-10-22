"""The M6 eval harness: failed calls become rows, results.json survives crashes and resumes,
fixtures resolve by id, cases.json stays portable, and compare/rescore respect the validator.

    python3 -m unittest discover tests      (from the repo root)

The director model is a local HTTP stub (modes: good / empty / http500 / timeout / malformed),
like the ComfyUI stubs elsewhere; no real model or GPU is involved. One test renders the
generated fixtures with the real ffmpeg (skipped when ffmpeg is absent).
"""
import hashlib, io, json, shutil, socket, subprocess, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import director, eval as evalh, h3check, rescore, eval_compare

GOOD = (ROOT / 'tests' / 'fixtures' / 'h3' / 't2va-boat.txt').read_text().strip()
CASES = ROOT / 'eval' / 'cases.json'


class ModelStub(BaseHTTPRequestHandler):
    """Canned chat-completions replies; mode is a class attribute per test."""
    mode = 'good'

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith('/models'):
            return self._reply(200, json.dumps({'data': [{'id': 'stub-model'}]}).encode())
        self._reply(404, b'{}')

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        mode = type(self).mode
        if mode == 'http500':
            self.send_error(500, 'stub model down')
            return
        if mode == 'timeout':
            import time
            time.sleep(5)
        if mode == 'malformed':
            self._reply(200, b'not json at all')
            return
        content = '' if mode == 'empty' else GOOD
        self._reply(200, json.dumps({'choices': [{'message': {'content': content}}],
                                     'usage': {'completion_tokens': 900}}).encode())


def start_stub(mode):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), ModelStub)
    ModelStub.mode = mode
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions'


def run_eval(url, out, cases=None, **kw):
    kw.setdefault('runs', 1)
    kw.setdefault('quiet', True)
    kw.setdefault('out_root', out)
    return evalh.run_eval('stublabel', url, 'stub-model', case_filter=cases, **kw)


def dead_port_url():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return f'http://127.0.0.1:{port}/v1/chat/completions'


class FailedCallRows(unittest.TestCase):
    """Every transport-level failure mode yields a failed_call row, is counted, and exits 1."""

    def check(self, mode, expect_class, expect_attempts, cases='C1-t2va-boat,C7-t2va-train', **kw):
        if mode == 'refused':
            url = dead_port_url()
            srv = None
        else:
            srv, url = start_stub(mode)
        try:
            with tempfile.TemporaryDirectory() as td:
                _, rows, code = run_eval(url, td, cases=cases, **kw)
                self.assertEqual(code, 1)
                self.assertEqual(len(rows), 2)
                for r in rows:
                    self.assertEqual(r['status'], 'failed_call')
                    self.assertEqual(r['error_class'], expect_class)
                    self.assertTrue(r['error'])
                    self.assertEqual((r['passed'], r['total']), (0, 0))
                    self.assertEqual(r['attempts'], expect_attempts)
                doc = json.loads((Path(td) / 'stublabel' / 'results.json').read_text())
                self.assertEqual(len(doc['rows']), 2)     # denominator covers the failures
        finally:
            if srv:
                srv.shutdown()

    def test_http500(self):
        self.check('http500', 'DirectorError', 1)

    def test_refused(self):
        self.check('refused', 'DirectorError', 1)

    def test_malformed_json(self):
        self.check('malformed', 'DirectorError', 1)

    def test_empty_reply_retries_then_fails(self):
        self.check('empty', 'DirectorError', 2)

    def test_timeout(self):
        self.check('timeout', 'DirectorError', 1, timeout=1)


class GoodRunRows(unittest.TestCase):
    def test_ok_rows_and_header(self):
        srv, url = start_stub('good')
        try:
            with tempfile.TemporaryDirectory() as td:
                _, rows, code = run_eval(url, td, cases='C1-t2va-boat')
                self.assertEqual(code, 0)
                self.assertEqual(rows[0]['status'], 'ok')
                self.assertEqual(rows[0]['attempts'], 1)
                self.assertTrue((Path(td) / 'stublabel' / 'C1-t2va-boat-1.txt').is_file())
                doc = json.loads((Path(td) / 'stublabel' / 'results.json').read_text())
                for key in ('schema_version', 'label', 'started', 'finished', 'director_url',
                            'configured_model', 'models_endpoint', 'call_settings', 'skill_revision',
                            'validator', 'git', 'python', 'host_hash', 'runs_per_case'):
                    self.assertIn(key, doc)
                self.assertEqual(doc['schema_version'], 2)
                self.assertEqual(doc['models_endpoint'], ['stub-model'])
                cs = doc['call_settings']
                for key in ('temperature', 'max_tokens', 'top_p', 'seed', 'skill_files'):
                    self.assertIn(key, cs)
                self.assertTrue(cs['skill_files'])
                v = doc['validator']
                self.assertEqual(v['h3check_version'], h3check.H3CHECK_VERSION)
                self.assertEqual(v['h3check_sha256'], evalh.sha256_file(ROOT / 'h3check.py'))
                self.assertEqual(v['cases_sha256'], evalh.sha256_file(CASES))
                self.assertEqual(v['eval_sha256'], evalh.sha256_file(ROOT / 'eval.py'))
                self.assertEqual(set(doc['git']), {'commit', 'dirty'})
                text = (Path(td) / 'stublabel' / 'results.json').read_text()
                self.assertNotIn(socket.gethostname(), text)
                self.assertNotIn(socket.gethostname(), str(doc['host_hash']))
                self.assertEqual(len(doc['host_hash']), 16)
        finally:
            srv.shutdown()


class CrashAndResume(unittest.TestCase):
    def test_incremental_write_and_resume(self):
        srv, url = start_stub('good')
        real_chat = director.chat
        state = {'n': 0}

        def exploding_chat(*a, **kw):
            state['n'] += 1
            if state['n'] == 2:
                raise KeyboardInterrupt
            return real_chat(*a, **kw)

        resume_calls = {'n': 0}

        def counting_chat(*a, **kw):
            resume_calls['n'] += 1
            return real_chat(*a, **kw)
        try:
            with tempfile.TemporaryDirectory() as td:
                with patch.object(director, 'chat', exploding_chat):
                    with self.assertRaises(KeyboardInterrupt):
                        run_eval(url, td, cases='C1-t2va-boat,C7-t2va-train')
                doc = json.loads((Path(td) / 'stublabel' / 'results.json').read_text())
                self.assertEqual([r['case'] for r in doc['rows']], ['C1-t2va-boat'])
                started = doc['started']
                with patch.object(director, 'chat', counting_chat):
                    _, rows, code = run_eval(url, td, cases='C1-t2va-boat,C7-t2va-train', resume=True)
                self.assertEqual(code, 0)
                self.assertEqual([r['case'] for r in rows], ['C1-t2va-boat', 'C7-t2va-train'])
                doc = json.loads((Path(td) / 'stublabel' / 'results.json').read_text())
                self.assertEqual(doc['started'], started)
                self.assertEqual(resume_calls['n'], 1)   # resume called the model once: never redid C1
        finally:
            srv.shutdown()

    def test_resume_refuses_changed_validator(self):
        srv, url = start_stub('good')
        try:
            with tempfile.TemporaryDirectory() as td:
                run_eval(url, td, cases='C1-t2va-boat')
                path = Path(td) / 'stublabel' / 'results.json'
                doc = json.loads(path.read_text())
                doc['validator']['h3check_version'] = h3check.H3CHECK_VERSION + 1
                path.write_text(json.dumps(doc))
                _, _, code = run_eval(url, td, cases='C1-t2va-boat', resume=True)
                self.assertEqual(code, 2)
        finally:
            srv.shutdown()

    def test_resume_refuses_schema1(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'stublabel').mkdir()
            (Path(td) / 'stublabel' / 'results.json').write_text('[]')
            _, _, code = run_eval(dead_port_url(), td, resume=True)
            self.assertEqual(code, 2)


class FixtureResolution(unittest.TestCase):
    def test_private_missing_is_skipped_row(self):
        with tempfile.TemporaryDirectory() as td:
            _, rows, code = run_eval(dead_port_url(), td, cases='C3-ref-lightsaber',
                                     fixture_dir=Path(td) / 'nofix')
            self.assertEqual(code, 0)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r['status'], 'skipped_fixture_missing')
            self.assertEqual(r['missing_fixture'], 'group')
            self.assertTrue(any('nofix' in p for p in r['searched']))
            self.assertTrue(any(p.endswith('group.jpg') and 'eval/fixtures' in p for p in r['searched']))

    def test_private_override_by_path(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / 'group-photo.jpg'
            fake.write_bytes(b'not really a photo, only its existence is checked')
            _, rows, code = run_eval(dead_port_url(), td, cases='C3-ref-lightsaber',
                                     fixture_dir=Path(td) / 'nofix',
                                     overrides={'EVAL_GROUP_IMAGE': str(fake)})
            self.assertEqual(code, 1)                      # fixture found -> model call -> refused
            self.assertEqual(rows[0]['status'], 'failed_call')

    def test_portable_skips_private_cases(self):
        with tempfile.TemporaryDirectory() as td:
            _, rows, code = run_eval(dead_port_url(), td,
                                     cases='C3-ref-lightsaber,C1-t2va-boat', portable=True,
                                     fixture_dir=Path(td) / 'nofix')
            self.assertEqual([r['case'] for r in rows], ['C1-t2va-boat'])
            self.assertEqual(rows[0]['status'], 'failed_call')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'needs ffmpeg')
    def test_generated_fixture_built_on_demand(self):
        srv, url = start_stub('good')
        try:
            with tempfile.TemporaryDirectory() as td:
                _, rows, code = run_eval(url, td, cases='C8-i2va-pizza', portable=True,
                                         fixture_dir=Path(td) / 'fixtures')
                png = Path(td) / 'fixtures' / 'generated' / 'team-pizza.png'
                self.assertTrue(png.is_file())
                self.assertEqual(rows[0]['status'], 'ok')
                self.assertEqual(code, 0)
        finally:
            srv.shutdown()


@unittest.skipUnless(shutil.which('ffmpeg'), 'needs ffmpeg')
class FixtureDeterminism(unittest.TestCase):
    def test_same_bytes_twice(self):
        outs = []
        for _ in range(2):
            td = tempfile.mkdtemp()
            outs.append(td)
            res = subprocess.run([sys.executable, str(ROOT / 'eval' / 'make_fixtures.py'), '--out', td],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
        for name in ('team-pizza.png', 'cafe-mug.png', 'reston-ranch.png'):
            a = hashlib.sha256((Path(outs[0]) / name).read_bytes()).hexdigest()
            b = hashlib.sha256((Path(outs[1]) / name).read_bytes()).hexdigest()
            self.assertEqual(a, b, f'{name} not reproducible')


class CaseSetPortability(unittest.TestCase):
    def test_no_machine_specific_paths(self):
        text = CASES.read_text()
        self.assertNotIn('/home', text)
        self.assertNotIn('~', text)
        self.assertNotIn('\\', text)
        data = json.loads(text)
        for spec in data['fixtures'].values():
            self.assertNotIn('/', spec['file'])
            self.assertEqual(Path(spec['file']).name, spec['file'])
        for case in data['cases']:
            for fid in evalh.case_fixture_ids(case):
                self.assertIn(fid, data['fixtures'])
        self.assertEqual(len({c['id'] for c in data['cases']}), len(data['cases']))

    def test_portable_subset_is_nonprivate(self):
        data = json.loads(CASES.read_text())
        fixtures = data['fixtures']
        portable = [c['id'] for c in data['cases'] if evalh.case_is_portable(c, fixtures)]
        self.assertGreaterEqual(len(portable), 6)
        for cid in portable:
            case = next(c for c in data['cases'] if c['id'] == cid)
            self.assertTrue(all(fixtures[f]['kind'] == 'generated'
                                for f in evalh.case_fixture_ids(case)))


class RescoreTests(unittest.TestCase):
    def test_schema1_flat_list(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / 'results.json').write_text(json.dumps(
                [dict(case='C1-t2va-boat', run=1, mode='t2va', passed=9, total=9, failed=[],
                      error=None, seconds=13.2)]))
            (td / 'C1-t2va-boat-1.txt').write_text(GOOD + '\n')
            s = rescore.score_label(td)
            doc = json.loads((td / 'rescored.json').read_text())
            self.assertEqual(s['schema'], 1)
            self.assertIsNone(s['validator']['recorded']['h3check_version'])
            self.assertEqual(s['validator']['current']['h3check_version'], h3check.H3CHECK_VERSION)
            self.assertFalse(s['validator_changed'])   # unknown recorded is not "changed"
            row = doc['rows'][0]
            self.assertIsNotNone(row['new_failed'])
            self.assertGreater(row['new_total'], 0)

    def test_schema2_keeps_failed_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            doc = dict(schema_version=2, label='x',
                       validator=dict(rescore.current_validator()),
                       rows=[dict(case='C1-t2va-boat', run=1, mode='t2va', status='ok',
                                  passed=9, total=9, failed=[], error=None),
                             dict(case='C7-t2va-train', run=1, status='failed_call', passed=0,
                                  total=0, failed=[], error='boom', error_class='DirectorError'),
                             dict(case='C3-ref-lightsaber', run=1, status='skipped_fixture_missing',
                                  passed=0, total=0, failed=[], error=None, missing_fixture='group')])
            (td / 'results.json').write_text(json.dumps(doc))
            (td / 'C1-t2va-boat-1.txt').write_text(GOOD + '\n')
            s = rescore.score_label(td)
            rd = json.loads((td / 'rescored.json').read_text())
            by = {r['case']: r for r in rd['rows']}
            self.assertIsNotNone(by['C1-t2va-boat']['new_failed'])
            self.assertIsNone(by['C7-t2va-train']['new_failed'])
            self.assertEqual(by['C7-t2va-train']['note'], 'kept as recorded')
            self.assertEqual(s['skipped'], 1)
            self.assertEqual(s['failed_calls'], 1)
            self.assertFalse(s['validator_changed'])


class CompareTests(unittest.TestCase):
    def docs(self, td):
        td = Path(td)
        val = dict(rescore.current_validator())
        rows = lambda b: [
            dict(case='C1-t2va-boat', run=1, mode='t2va', status='ok', passed=9, total=9, failed=[]),
            dict(case='C7-t2va-train', run=1, mode='t2va', status='ok', passed=8, total=10,
                 failed=['mentions:steam'] if b else []),
            dict(case='C3-ref-lightsaber', run=1, mode='ref2va', status='ok', passed=11, total=11,
                 failed=[]) if not b else
            dict(case='C3-ref-lightsaber', run=1, status='failed_call', passed=0, total=0,
                 failed=[], error='boom', error_class='DirectorError')]
        for name, b in (('A', False), ('B', True)):
            d = td / name
            d.mkdir()
            (d / 'results.json').write_text(json.dumps(dict(schema_version=2, label=name,
                                                            validator=val, rows=rows(b))))
        return td / 'A', td / 'B'

    def test_diffs_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = self.docs(td)
            out = io.StringIO()
            code = eval_compare.compare([a, b], stream=out)
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn('C7-t2va-train run1: A: PASS  |  B: FAIL mentions:steam', text)
            self.assertIn('C3-ref-lightsaber run1: A: PASS  |  B: FAILED_CALL', text)
            self.assertIn('B: 1/3 case-runs passed, 1 failed calls, 0 skipped', text)
            self.assertIn('A: 3/3 case-runs passed, 0 failed calls, 0 skipped', text)

    def test_refuses_mismatched_validator(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = self.docs(td)
            doc = json.loads((b / 'results.json').read_text())
            doc['validator']['h3check_version'] = 1
            (b / 'results.json').write_text(json.dumps(doc))
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, 'stderr', err):
                self.assertEqual(eval_compare.compare([a, b], stream=out), 2)
            self.assertIn('refusing', err.getvalue())
            with patch.object(sys, 'stderr', io.StringIO()) as forced:
                self.assertEqual(eval_compare.compare([a, b], force=True, stream=io.StringIO()), 0)
            self.assertIn('WARNING', forced.getvalue())

    def test_rescored_json_makes_schema1_comparable(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = self.docs(td)
            doc = json.loads((a / 'results.json').read_text())
            rows = [dict(case=r['case'], run=r['run'], mode=r.get('mode'), status=r['status'],
                         old_passed=r['passed'], old_total=r['total'], old_failed=r['failed'],
                         new_passed=r['passed'], new_total=r['total'], new_failed=r['failed'],
                         newly_failed=[]) for r in doc['rows']]
            (a / 'results.json').write_text(json.dumps(rows))     # schema 1: flat, no validator
            (a / 'rescored.json').write_text(json.dumps(
                dict(summary=dict(validator={'recorded': {'h3check_version': None},
                                             'current': dict(rescore.current_validator())}),
                     rows=rows)))
            out = io.StringIO()
            self.assertEqual(eval_compare.compare([a, b], stream=out), 0)
            self.assertIn('rescored.json', out.getvalue())


if __name__ == '__main__':
    unittest.main()
