"""Recovery must tolerate slow local ledger reads, never infer readiness."""
import ast
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starlette.responses import JSONResponse
from utils import refinery_recovery as recovery


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.delay = 0
        self.status = 200
        self.payload = {"status": "ok"}
        case = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.calls.append(self.path)
                time.sleep(case.delay)
                self.send_response(case.status)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(case.payload).encode())
                except BrokenPipeError:
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'kg_hub_server.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'model_readiness')
        ns = {'Request': object, 'JSONResponse': JSONResponse,
              'gateway_base_url': lambda: f'http://127.0.0.1:{self.server.server_port}'}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<readiness>', 'exec'), ns)
        self.probe = ns['model_readiness']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_healthy_read_slower_than_old_five_second_limit_recovers(self):
        self.delay = 5.2
        response = asyncio.run(self.probe(None))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, ['/health/ready'])

    def test_failure_bad_body_and_timeout_stay_paused(self):
        for status, payload in [(503, {'status': 'ok'}), (200, {'status': 'error'})]:
            self.status, self.payload = status, payload
            self.assertEqual(asyncio.run(self.probe(None)).status_code, 503)
        self.status, self.payload, self.delay = 200, {'status': 'ok'}, .1
        with patch.object(recovery, 'GATEWAY_READINESS_TIMEOUT', .02):
            self.assertEqual(asyncio.run(self.probe(None)).status_code, 503)
        self.assertEqual(self.calls, ['/health/ready'] * 3)


if __name__ == '__main__':
    unittest.main()
