import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from hermes_runtime import GatewayRuntime


class Handler(BaseHTTPRequestHandler):
    calls = []
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append((self.path, dict(self.headers), data))
        body = json.dumps({'choices': [{'message': {'content': 'mock reply'}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass


class GatewayTests(unittest.TestCase):
    def test_default_only_private_key(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            key = Path(tmp) / 'key'; key.write_text('test-only'); key.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'default'):
                GatewayRuntime('other', 'http://127.0.0.1:8765', key)
            key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private'):
                GatewayRuntime('default', 'http://127.0.0.1:8765', key)
            with self.assertRaisesRegex(ValueError, 'local'):
                GatewayRuntime('default', 'https://remote.example', key)

    def test_declared_session_without_profile_override(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                key = Path(tmp) / 'key'; key.write_text('test-only'); key.chmod(0o600)
                # Keep the production fixed-port restriction; only the transport is mocked.
                from unittest.mock import patch
                import httpx
                real_client = httpx.AsyncClient
                def client_factory(*args, **kwargs):
                    return real_client(*args, **kwargs, transport=httpx.AsyncHTTPTransport())
                with patch('hermes_runtime.GatewayRuntime.__init__', autospec=True) as init:
                    init.side_effect = lambda self, profile, url, file, deadline=90: None
                    runtime = GatewayRuntime('default', '', '')
                runtime.profile='default'; runtime.url=f'http://127.0.0.1:{server.server_port}'
                runtime.key_file=key; runtime.deadline=5
                runtime.slots=asyncio.Semaphore(2); runtime.session_locks={}
                Handler.calls=[]
                async def check():
                    self.assertEqual(await runtime('hello', 'session-a'), 'mock reply')
                    self.assertEqual(await runtime('again', 'session-a'), 'mock reply')
                asyncio.run(check())
                self.assertEqual(len(Handler.calls), 2)
                self.assertEqual(Handler.calls[0][1]['X-Hermes-Session-Key'], Handler.calls[1][1]['X-Hermes-Session-Key'])
                self.assertNotIn('X-Hermes-Profile', Handler.calls[0][1])
                self.assertEqual(Handler.calls[0][2]['messages'][-1], {'role':'user','content':'hello'})
                self.assertEqual(Handler.calls[0][2]['messages'][0]['role'], 'system')
                self.assertIn('40-second audio limit', Handler.calls[0][2]['messages'][0]['content'])
        finally:
            server.shutdown(); thread.join(); server.server_close()
