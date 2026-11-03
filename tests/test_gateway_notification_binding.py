import asyncio
import importlib.util

import httpx
from connector import Store, create_app
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GatewayNotificationBindingTest(unittest.TestCase):
    def test_private_binding_and_cross_profile_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = root / 'gateway'
            foreign = root / 'foreign'
            bridge = root / 'bridge'
            for directory in (gateway, foreign, bridge):
                directory.mkdir(mode=0o700)
            binding = root / 'binding.json'
            data = dict(mode='gateway', gateway_home=str(gateway), bridge_home=str(bridge),
                        db=str(bridge / 'sim-inbox.sqlite'), owner='owner-a', profile='default')
            binding.write_text(json.dumps(data))
            binding.chmod(0o600)
            spec = importlib.util.spec_from_file_location('sim_gateway_binding_test', ROOT / '__init__.py',
                                                          submodule_search_locations=[str(ROOT)])
            module = importlib.util.module_from_spec(spec)
            import sys
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            args = dict(owner='owner-a', profile='default', event_id='synthetic-1',
                        kind='question', title='Synthetic', body='Only a test')
            with patch.dict(os.environ, {'HERMES_HOME': str(gateway), 'SIM_BRIDGE_BINDING_FILE': str(binding)}, clear=True):
                self.assertTrue(module.configured_scope())
                self.assertTrue(json.loads(module.notify(args))['success'])
                self.assertEqual(json.loads(module.notify(args))['id'], json.loads(module.notify(args))['id'])
                store = Store(data['db'])
                token = store.confirm(store.pair('owner-a', 'default', 'orb'), 'default', 'orb')
                async def readback():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store)), base_url='http://localhost') as client:
                        self.assertEqual((await client.get('/v1/inbox/meta')).status_code, 401)
                        headers = {'Authorization': 'Bearer ' + token}
                        meta = await client.get('/v1/inbox/meta', headers=headers)
                        self.assertEqual(meta.json()['unread'], 1)
                        page = await client.get('/v1/inbox', headers=headers)
                        self.assertEqual(len(page.json()['items']), 1)
                        item = page.json()['items'][0]
                        self.assertEqual((await client.post('/v1/inbox/' + item['id'] + '/open', headers=headers)).status_code, 200)
                        self.assertEqual((await client.get('/v1/inbox/meta', headers=headers)).json()['unread'], 0)
                        store.revoke('owner-a', 'default', 'orb')
                        self.assertEqual((await client.get('/v1/inbox/meta', headers=headers)).status_code, 401)
                asyncio.run(readback())
                self.assertEqual(store.db.execute('select count(*) from messages').fetchone()[0], 1)
                store.db.close()
                self.assertFalse(json.loads(module.notify(dict(args, profile='other')))['success'])
                os.environ['HERMES_HOME'] = str(foreign)
                self.assertFalse(module.configured_scope())
                self.assertFalse(json.loads(module.notify(args))['success'])
                os.environ['HERMES_HOME'] = str(gateway)
                data['db'] = str(foreign / 'sim-inbox.sqlite')
                binding.write_text(json.dumps(data))
                self.assertFalse(module.configured_scope())
                data['db'] = str(bridge / 'sim-inbox.sqlite')
                binding.write_text(json.dumps(data))
                binding.chmod(0o644)
                self.assertFalse(module.configured_scope())
                check = Store(bridge / 'sim-inbox.sqlite')
                self.assertEqual(check.db.execute('select count(*) from messages').fetchone()[0], 1)
                check.db.close()
