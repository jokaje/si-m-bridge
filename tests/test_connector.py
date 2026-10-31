import asyncio
import base64
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
import httpx
import uvicorn
from urllib.parse import urlencode
from unittest.mock import patch
from connector import Store, create_app, safe_url

class ConnectorTests(unittest.TestCase):
    def test_android_pairing_qr_fixture_generated_by_server(self):
        fixture=Path(__file__).resolve().parents[2]/'si-m-connector'/'project'/'app'/'src'/'test'/'resources'/'connector'/'server-pair-qr.txt'
        with tempfile.TemporaryDirectory(dir=__import__('os').environ.get('TMPDIR')) as tmp:
            store=Store(Path(tmp)/'qr.sqlite')
            with patch('connector.secrets.token_urlsafe',return_value='SyntheticPairingCode_0123456789abcdefghijk'):
                code=store.pair('synthetic','kim','orb')
            expected='sim://pair?'+urlencode({'v':1,'url':'https://connector.example','code':code,'profile':'kim','device':'orb'})
            self.assertEqual(fixture.read_text().strip(),expected)
            store.db.close()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)/'store.db')
        self.app=create_app(self.store)
    def tearDown(self): self.store.db.close(); self.tmp.cleanup()
    def call(self, method, path, token=None, **kw):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://localhost') as c:
                return await c.request(method,path,headers={'Authorization':'Bearer '+token} if token else {},**kw)
        return asyncio.run(run())
    def paired(self,owner='a',profile='kai',device='orb'):
        code=self.store.pair(owner,profile,device)
        response=self.call('POST','/v1/pair/confirm',json={'version':1,'code':code,'profile':profile,'device':device})
        self.assertEqual(response.status_code,200)
        return response.json()['token']
    def test_pair_one_use_expiry_and_wrong_scope(self):
        code=self.store.pair('a','kai','orb',now=100)
        self.assertIsNone(self.store.confirm(code,'other','orb',now=101))
        token=self.store.confirm(code,'kai','orb',now=101)
        self.assertIsNotNone(token)
        self.assertIsNone(self.store.confirm(code,'kai','orb',now=101))
        self.assertIsNone(self.store.confirm(self.store.pair('a','kai','new',now=100),'kai','new',now=220))
        self.assertIsNone(self.store.auth('invalid'))
    def test_concurrent_one_use_across_connections(self):
        code=self.store.pair('a','kai','orb')
        results=[]
        def run():
            db=Store(Path(self.tmp.name)/'store.db')
            try: results.append(db.confirm(code,'kai','orb'))
            finally: db.db.close()
        threads=[threading.Thread(target=run) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(x is not None for x in results),1)
    def test_http_scope_ack_persistence_revocation(self):
        a=self.paired(); b=self.paired(owner='b',device='other')
        ident=self.store.put('a','kai','task-1','task_done','Done','Synthetic body')
        self.assertEqual(self.call('GET','/v1/inbox/meta').status_code,401)
        self.assertEqual(self.call('GET','/v1/inbox/meta',b).json()['unread'],0)
        self.assertEqual(self.call('GET','/v1/inbox',a).json()['items'][0]['body'],'Synthetic body')
        self.assertEqual(self.call('GET','/v1/inbox/meta',a).json()['unread'],1)
        self.assertEqual(self.call('POST',f'/v1/inbox/{ident}/open',b).status_code,404)
        self.assertEqual(self.call('POST',f'/v1/inbox/{ident}/open',a).status_code,200)
        self.assertEqual(self.call('POST',f'/v1/inbox/{ident}/open',a).status_code,200)
        self.assertEqual(self.call('GET','/v1/inbox/meta',a).json()['unread'],0)
        self.assertTrue(self.store.revoke('a','kai','orb'))
        self.assertEqual(self.call('GET','/v1/inbox',a).status_code,401)
    def test_wire_cursor_binding_and_opened_contract(self):
        token=self.paired()
        scope=self.store.auth(token)
        self.store.put('a','kai','seed','question','Seed','Synthetic')
        self.store.db.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?',(2**32-2,'messages'))
        ident=self.store.put('a','kai','boundary','question','Boundary','Synthetic')
        meta=self.call('GET','/v1/inbox/meta',token).json()
        self.assertEqual(meta['cursor'],str(2**32-1))
        self.assertRegex(meta['binding'],r'^[0-9a-f]{32}$')
        first=self.call('GET','/v1/inbox',token,params={'cursor':str(2**32-2)}).json()
        self.assertEqual(first['cursor'],meta['cursor'])
        self.assertEqual(first['binding'],meta['binding'])
        self.assertIs(first['items'][0]['opened'],False)
        self.assertIsNone(first['items'][0]['openedAt'])
        self.assertEqual(self.call('POST',f'/v1/inbox/{ident}/open',token).status_code,200)
        self.assertIs(self.call('GET','/v1/inbox',token,params={'cursor':str(2**32-2)}).json()['items'][0]['opened'],True)
        for bad in ('4294967295.0','-1','01',str(2**63),'1e2'):
            self.assertEqual(self.call('GET','/v1/inbox',token,params={'cursor':bad}).status_code,400)
        replacement=self.paired()
        self.assertNotEqual(meta['binding'],self.call('GET','/v1/inbox/meta',replacement).json()['binding'])
        self.assertEqual(self.call('GET','/v1/inbox/meta',token).status_code,401)

    def test_profile_label_is_not_upstream_attestation(self):
        token=self.paired()
        self.app=create_app(self.store,'http://127.0.0.1:8765','synthetic',hermes_profile='kai')
        self.assertEqual(self.call('POST','/v1/turn',token,json={'version':1,'text':'test'}).status_code,403)

    def test_audio_requires_recognizer_and_authorization(self):
        token=self.paired()
        payload={'version':1,'pcm16le_b64':base64.b64encode(b'\0\0'*100).decode(),'sample_rate':16000}
        self.assertEqual(self.call('POST','/v1/turn-stream',json=payload).status_code,401)
        self.assertEqual(self.call('POST','/v1/turn-stream',token,json=payload).status_code,403)
        self.assertEqual(self.call('POST','/v1/turn',token,json={'version':1,'text':'test'}).status_code,403)
    def test_bounds_idempotence_and_url(self):
        self.assertEqual(self.store.put('a','kai','x','question','Title','First'),self.store.put('a','kai','x','question','Title','Changed'))
        self.assertEqual(self.call('POST','/v1/pair/confirm',json={'x':'z'*100000}).status_code,413)
        with self.assertRaises(ValueError): safe_url('http://192.168.1.2:8766')
        self.assertEqual(safe_url('http://127.0.0.1:8766'),'http://127.0.0.1:8766')
        for url in ('https://@example.com','https://example.com:bad','https://example.com:99999','https://example.com\\evil','https://example.com%2f.evil','https://example.com//evil'):
            with self.subTest(url=url), self.assertRaises(ValueError): safe_url(url)

    def test_revoke_and_repair_rotates_session(self):
        first=self.paired()
        session=self.store.session(self.store.auth(first))
        self.assertTrue(self.store.revoke('a','kai','orb'))
        second=self.paired()
        self.assertIsNone(self.store.auth(first))
        self.assertNotEqual(session,self.store.session(self.store.auth(second)))

    def test_real_loopback_http_and_profile_gate(self):
        sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(8)
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(self.app,log_level='critical',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.01)
            self.assertTrue(server.started)
            token=self.paired()
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3) as c:
                self.assertEqual(c.get('/v1/inbox/meta',headers={'Authorization':'Bearer '+token}).json()['unread'],0)
                self.assertEqual(c.post('/v1/turn',headers={'Authorization':'Bearer '+token},json={'version':1,'text':'test'}).status_code,403)
        finally:
            server.should_exit=True; thread.join(timeout=5); sock.close()
            self.assertFalse(thread.is_alive())

if __name__=='__main__': unittest.main()
