import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from connector import Store, create_app
from hermes_runtime import HermesRuntime
import httpx

ROOT = Path(__file__).resolve().parents[1]

class Provider(BaseHTTPRequestHandler):
    requests = []
    blocked = None
    entered = None
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((self.path, body))
        if self.blocked and any(self.blocked in str(m.get('content', '')) for m in body.get('messages', [])):
            self.entered.set()
            time.sleep(15)
        if body.get('stream'):
            chunks = [dict(index=0,delta={'role':'assistant','content':'Synthetic answer'},finish_reason=None),
                      dict(index=0,delta={},finish_reason='stop')]
            payload = b''.join(b'data: '+json.dumps({'id':'synthetic','object':'chat.completion.chunk','model':'synthetic','choices':[chunk]}).encode()+b'\n\n' for chunk in chunks)+b'data: [DONE]\n\n'
            content_type='text/event-stream'
        else:
            payload = json.dumps({'id':'synthetic','object':'chat.completion','model':'synthetic',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'Synthetic answer'}}],
                'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}}).encode()
            content_type='application/json'
        self.send_response(200); self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def log_message(self,*args): pass

class RuntimeTests(unittest.TestCase):
    def test_real_hermes_agent_in_blank_home_with_connector(self):
        original_tempdir = tempfile.tempdir
        original_tmpdir = os.environ.get('TMPDIR')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            home=Path(tmp); home.chmod(0o700)
            plugin=home/'plugins'/'si-m-bridge'; plugin.mkdir(parents=True)
            for name in ('__init__.py','connector.py','audio_bridge.py','plugin.yaml'):
                shutil.copy(ROOT/name,plugin/name)
            server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
            (home/'config.yaml').write_text(f'model:\n  provider: custom\n  default: synthetic\n  base_url: http://127.0.0.1:{server.server_port}/v1\n  api_key: synthetic\nplugins:\n  enabled: [si-m-bridge]\n  disabled: []\nplatform_toolsets:\n  cli: [sim_bridge]\nmemory:\n  memory_enabled: true\n  user_profile_enabled: false\n')
            (home/'SOUL.md').write_text('ISOLATED_SOUL_MARKER')
            (home/'memories').mkdir()
            (home/'memories'/'MEMORY.md').write_text('ISOLATED_MEMORY_MARKER')
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            Provider.requests=[]
            try:
                with patch.dict(os.environ,{'HERMES_HOME':str(home),'SIM_CONNECTOR_HERMES_HOME':str(home),'SIM_CONNECTOR_DB':str(home/'sim-inbox.sqlite'),'SIM_CONNECTOR_OWNER':'owner','SIM_CONNECTOR_PROFILE':'synthetic'}):
                    runtime=HermesRuntime('synthetic',str(home))
                    store=Store(home/'connector.sqlite')
                    token=store.confirm(store.pair('owner','synthetic','orb'),'synthetic','orb')
                    app=create_app(store,hermes_profile='synthetic',turn_runner=runtime)
                    async def check():
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                            for prompt in ('Synthetic hello','Synthetic follow-up'):
                                response=await client.post('/v1/turn',headers={'Authorization':'Bearer '+token},json={'version':1,'text':prompt})
                                self.assertEqual(response.status_code,200,response.text)
                                self.assertEqual(response.json()['reply'],'Synthetic answer')
                    asyncio.run(check())
                    self.assertGreaterEqual(len(Provider.requests),2)
                    self.assertGreaterEqual(sum(path=='/v1/chat/completions' for path,_ in Provider.requests),2)
                    prompts=json.dumps([body for _,body in Provider.requests])
                    self.assertIn('ISOLATED_SOUL_MARKER',prompts)
                    self.assertIn('ISOLATED_MEMORY_MARKER',prompts)
                    self.assertIn('sim_notify',prompts)

                    runtime.close(); store.db.close()
            finally:
                Provider.blocked = None
                server.shutdown(); thread.join(); server.server_close()
                tempfile.tempdir = original_tempdir
                if original_tmpdir is not None: os.environ['TMPDIR'] = original_tmpdir

    def test_real_worker_timeout_cancel_busy_and_recovery(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            home = Path(tmp); home.chmod(0o700)
            plugin = home/'plugins'/'si-m-bridge'; plugin.mkdir(parents=True)
            for name in ('__init__.py', 'connector.py', 'audio_bridge.py', 'plugin.yaml'):
                shutil.copy(ROOT/name, plugin/name)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            (home/'config.yaml').write_text(
                f'model:\n  provider: custom\n  default: synthetic\n  base_url: http://127.0.0.1:{server.server_port}/v1\n  api_key: synthetic\n'
                'plugins:\n  enabled: [si-m-bridge]\n  disabled: []\nplatform_toolsets:\n  cli: []\n'
                'memory:\n  memory_enabled: false\n  user_profile_enabled: false\n')
            Provider.blocked = 'BLOCK_WORKER'; Provider.entered = threading.Event()
            try:
                with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                    runtime = HermesRuntime('synthetic', str(home), deadline=7)
                    async def exercise():
                        first = asyncio.create_task(runtime('BLOCK_WORKER', 'session-a'))
                        second = asyncio.create_task(runtime('BLOCK_WORKER', 'session-b'))
                        await asyncio.wait_for(asyncio.to_thread(Provider.entered.wait), 8)
                        self.assertTrue(Provider.entered.is_set())
                        with self.assertRaisesRegex(ValueError, 'busy'):
                            await runtime('third', 'session-c')
                        first.cancel()
                        with self.assertRaises(asyncio.CancelledError): await first
                        with self.assertRaises(asyncio.TimeoutError): await second
                        self.assertEqual(runtime.slots._value, 2)
                        self.assertEqual(await runtime('recovered', 'session-d'), 'Synthetic answer')
                        self.assertEqual(runtime.slots._value, 2)
                    asyncio.run(exercise())
            finally:
                Provider.blocked = None
                server.shutdown(); thread.join(); server.server_close()
