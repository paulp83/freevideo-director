"""History entries captured from a REAL ComfyUI 0.34.0 (tests/fixtures/comfy_history/*.json) must classify the way
the director needs. A stub that guesses the shape cannot catch a misread: this suite once passed while a genuinely
failed prompt was reported as 'interrupted'."""
import json, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import enhance
import web

FIX = HERE / 'fixtures' / 'comfy_history'


def _serve(entries):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            pid = self.path.rsplit('/', 1)[-1]
            body = json.dumps({pid: entries[pid]} if pid in entries else {}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class RealHistoryShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cap = {k: json.loads((FIX / f'{k}.json').read_text()) for k in ('completed', 'failed')}
        entries = {c['prompt_id']: c['entry'] for c in cls.cap.values()}
        interrupted = {'status': {'status_str': 'error', 'completed': False, 'messages': [
            ['execution_start', {'timestamp': 1}], ['execution_interrupted', {'node_id': '1', 'timestamp': 2}]]},
            'outputs': {}}
        entries['11111111-1111-4111-8111-111111111111'] = interrupted   # ComfyUI source: interrupt also yields status_str 'error'
        cls.srv = _serve(entries)
        cls.saved = (web.COMFY, enhance.COMFY)
        web.COMFY = enhance.COMFY = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        web.COMFY, enhance.COMFY = cls.saved
        cls.srv.shutdown(); cls.srv.server_close()

    def test_a_real_success_is_completed(self):
        self.assertEqual(web.comfy_history_detail(self.cap['completed']['prompt_id']), ('completed', None))

    def test_a_real_runtime_failure_is_failed_with_comfyuis_message(self):
        outcome, message = web.comfy_history_detail(self.cap['failed']['prompt_id'])
        self.assertEqual(outcome, 'failed')
        self.assertIn('cannot identify image file', message)

    def test_an_interrupt_is_interrupted_not_failed(self):
        # status_str is 'error' for an interrupt too, so only the message kind separates the two
        self.assertEqual(web.comfy_history_detail('11111111-1111-4111-8111-111111111111')[0], 'interrupted')

    def test_no_entry_is_lost(self):
        self.assertEqual(web.comfy_history_detail('22222222-2222-4222-8222-222222222222'), ('lost', None))

    def test_real_entries_carry_the_timing_stamps_the_pilot_reads(self):
        msgs = [m[0] for m in self.cap['completed']['entry']['status']['messages']]
        self.assertIn('execution_start', msgs)
        self.assertIn('execution_success', msgs)


if __name__ == '__main__':
    unittest.main()


class RealObjectInfoShapes(unittest.TestCase):
    """Combo inputs captured from the real ComfyUI: RIFE lists its models as [[names], {default}], a shape a
    stub-only suite never produced (readiness called the installed rife49.pth 'unconfirmed')."""

    @classmethod
    def setUpClass(cls):
        import readiness
        cls.readiness = readiness
        cls.nodes = json.loads((HERE / 'fixtures' / 'comfy_object_info' / 'finishing_and_render_nodes.json')
                               .read_text())['nodes']

    def test_rife_models_are_read_from_the_legacy_combo_shape(self):
        options = self.readiness._options(self.nodes, 'RIFE VFI')
        self.assertIn('rife49.pth', options)

    def test_seedvr2_models_are_read_from_the_real_shape(self):
        self.assertIn('ema_vae_fp16.safetensors', self.readiness._options(self.nodes, 'SeedVR2LoadVAEModel'))
        self.assertTrue(any(o.endswith('.safetensors') for o in self.readiness._options(self.nodes, 'SeedVR2LoadDiTModel')))

    def test_a_node_with_no_combo_inputs_still_yields_none(self):
        self.assertIsNone(self.readiness._options({'X': {'input': {'required': {'a': ['INT', {}], 'b': ['IMAGE']}}}}, 'X'))
