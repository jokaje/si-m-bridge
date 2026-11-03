"""Synthetic adapter contract through actual HTTP/1.1 server bytes (not hardware)."""
import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from connector import Store, create_app


async def stt(pcm):
    return 'synthetic transcript'


async def turn(text, session):
    return 'synthetic reply'


async def tts(text):
    return b'\x01\x00' * 1600


class WireContract(unittest.TestCase):
    def test_raw_http_and_frame_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite')
            token = store.confirm(store.pair('owner', 'default', 'orb'), 'default', 'orb')
            app = create_app(store, transcribe=stt, hermes_profile='default', synthesize=tts,
                             turn_runner=turn)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sock.listen(5)
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                  log_level='critical', lifespan='off'))
            thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(server.started)
                body = b'\x00\x00' * 1600
                request = (f'POST /v1/turn-stream HTTP/1.1\r\nHost: localhost\r\n'
                           f'Authorization: Bearer {token}\r\n'
                           'Content-Type: application/vnd.kim.pcm;rate=16000;channels=1;format=s16le\r\n'
                           f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
                with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                    client.settimeout(10)
                    # Intentionally fragment request headers and payload.
                    for i in range(0, len(request), 37):
                        client.sendall(request[i:i+37])
                    raw = bytearray()
                    while True:
                        chunk = client.recv(8192)
                        if not chunk:
                            break
                        raw.extend(chunk)
                headers, payload = bytes(raw).split(b'\r\n\r\n', 1)
                self.assertIn(b'HTTP/1.1 200', headers)
                self.assertIn(b'application/vnd.kim.stream-v1', headers.lower())
                fields = dict(line.split(b':', 1) for line in headers.lower().split(b'\r\n')[1:])
                if b'transfer-encoding' in fields:
                    self.assertEqual(fields[b'transfer-encoding'].strip(), b'chunked')
                    decoded = bytearray()
                    while payload:
                        length, payload = payload.split(b'\r\n', 1)
                        n = int(length.split(b';', 1)[0], 16)
                        if n == 0:
                            self.assertEqual(payload, b'\r\n')
                            break
                        decoded.extend(payload[:n])
                        self.assertEqual(payload[n:n+2], b'\r\n')
                        payload = payload[n+2:]
                    payload = bytes(decoded)
                elif b'content-length' in fields:
                    self.assertEqual(len(payload), int(fields[b'content-length']))
                self.assertEqual(payload[:6], b'M\x01\x00\x00\x00\x01')
                frames = []
                offset = 0
                while offset < len(payload):
                    kind = payload[offset:offset+1]
                    length = int.from_bytes(payload[offset+1:offset+5], 'little')
                    offset += 5
                    self.assertLessEqual(length, 256000)
                    frames.append((kind, payload[offset:offset+length]))
                    offset += length
                self.assertEqual(offset, len(payload))
                self.assertEqual([k for k, _ in frames], [b'M', b'A', b'E'])
                self.assertEqual(len(frames[1][1]), 3200)
                self.assertEqual(frames[-1][1], b'')
                # This is an actual Uvicorn response, not the downstream nginx response.
            finally:
                server.should_exit = True
                thread.join(5)
                sock.close()
                store.db.close()
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
