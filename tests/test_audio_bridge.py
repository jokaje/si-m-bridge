import asyncio
import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from audio_bridge import AudioUnavailable, CommandAdapter, frames, pcm_from_wav
from connector import Store, create_app


class AudioBridgeTests(unittest.TestCase):
    def test_wire_frames_and_wav_conversion(self):
        stream=b''.join(frames(b'\x01\x00'*40000))
        self.assertEqual(stream[:6],b'M\x01\x00\x00\x00\x01')
        self.assertEqual(stream[6:11],b'A\x80\x38\x01\x00')
        self.assertEqual(stream[-5:],b'E\0\0\0\0')
        with io.BytesIO() as out:
            with wave.open(out,'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(struct.pack('<hhhhh',0,100,200,300,400))
            self.assertEqual(len(pcm_from_wav(out.getvalue())),6)
        with self.assertRaises(AudioUnavailable): frames(b'\0')

    def test_command_adapter_bounded_and_no_shell(self):
        with self.assertRaises(ValueError): CommandAdapter('echo hi')
        self.assertEqual(CommandAdapter(['/usr/bin/printf','ok'])(b''),b'ok')
        with self.assertRaises(AudioUnavailable): CommandAdapter(['/usr/bin/printf','123'],max_output=2)(b'')

    def test_pcm_to_hermes_to_binary_and_device_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite')
            first=store.confirm(store.pair('owner','kai','orb'),'kai','orb')
            second=store.confirm(store.pair('owner','kai','orb2'),'kai','orb2')
            calls=[]
            async def fake_turn(url,key,text,session):
                calls.append((text,session,key))
                return 'synthetic Hermes reply'
            app=create_app(store,'http://127.0.0.1:8765','master-only',lambda pcm:'synthetic transcript',
                           'kai',lambda text:b'\x02\0'*1600,
                           gateway_attestation=lambda profile,url: profile=='kai' and url=='http://127.0.0.1:8765')
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
                    outputs=[]
                    for token in (first,first,second):
                        outputs.append(await c.post('/v1/turn-stream',content=b'\x01\0'*1600,
                           headers={'Authorization':'Bearer '+token,'Content-Type':'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'}))
                    return outputs
            with patch('connector.hermes_turn',fake_turn): outputs=asyncio.run(run())
            self.assertEqual([r.status_code for r in outputs],[200]*3)
            self.assertTrue(all(r.content.startswith(b'M\x01\0\0\0\x01A') and r.content.endswith(b'E\0\0\0\0') for r in outputs))
            self.assertEqual(calls[0][1],calls[1][1]); self.assertNotEqual(calls[0][1],calls[2][1])
            self.assertEqual({x[2] for x in calls},{'master-only'})
            store.db.close()

if __name__=='__main__': unittest.main()
