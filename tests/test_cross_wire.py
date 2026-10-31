"""Compile the production firmware cursor decoder against real server responses."""
import asyncio
import ctypes
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import httpx
from connector import Store, create_app

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / 'notification-firmware/project'


class CrossWire(unittest.TestCase):
    def test_server_json_through_actual_c_cursor_decoder(self):
        source = (FIRMWARE / 'components/muse/orb_notifications_client.c').read_text()
        begin = source.index('static bool cursor_field(')
        end = source.index('static bool text_field(', begin)
        decoder = source[begin:end].replace('static bool cursor_field(', 'bool cursor_field(')
        cjson = FIRMWARE / 'managed_components/espressif__cjson/cJSON'
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp) / 'bridge.c'
            library = Path(tmp) / 'bridge.so'
            c.write_text('#include <stdbool.h>\n#include <stdint.h>\n#include <limits.h>\n#include <string.h>\n#include "cJSON.h"\n' + decoder + '''
                bool parse(const char *json,const char *field,uint64_t *out) {
                    cJSON *root=cJSON_Parse(json);
                    bool ok=root && cursor_field(root,field,out);
                    cJSON_Delete(root); return ok;
                }
            ''')
            subprocess.run(['cc', '-shared', '-fPIC', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I', str(cjson), str(c), str(cjson / 'cJSON.c'), '-o', str(library)], check=True)
            parse = ctypes.CDLL(str(library)).parse
            parse.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64)]
            parse.restype = ctypes.c_bool
            store = Store(Path(tmp) / 'store.db')
            try:
                code = store.pair('owner', 'profile', 'orb')
                app = create_app(store)
                async def run():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                        response = await client.post('/v1/pair/confirm', json={'version':1,'code':code,'profile':'profile','device':'orb'})
                        token = response.json()['token']
                        store.put('owner','profile','seed','question','Seed','Synthetic')
                        store.db.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?',(2**32-1,'messages'))
                        store.put('owner','profile','boundary','question','Boundary','Synthetic')
                        headers={'Authorization':'Bearer '+token}
                        return [(await client.get(path,headers=headers)).content for path in
                                ('/v1/inbox/meta','/v1/inbox?cursor=4294967295&limit=1')]
                for wire in asyncio.run(run()):
                    value=ctypes.c_uint64()
                    self.assertTrue(parse(wire,b'cursor',ctypes.byref(value)))
                    self.assertEqual(value.value,2**32)
                    self.assertEqual(len(json.loads(wire)['binding']),32)
                for wire in (b'{"cursor":4294967296}',b'{"cursor":"9223372036854775808"}',b'{"cursor":"01"}'):
                    self.assertFalse(parse(wire,b'cursor',ctypes.byref(ctypes.c_uint64())))
            finally:
                store.db.close()
