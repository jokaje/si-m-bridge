import asyncio
import os
import sys
import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from audio_bridge import AudioUnavailable, CommandAdapter, WhisperLocal, frames, pcm_from_wav
from connector import Store, create_app, _call


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

    def test_command_tts_text_and_wav_are_pcm(self):
        with io.BytesIO() as out:
            with wave.open(out, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(struct.pack('<hh', 123, -456))
            payload = out.getvalue()
        script = 'import sys; assert sys.stdin.buffer.read() == "Hallo".encode(); sys.stdout.buffer.write(%r)' % payload
        self.assertEqual(CommandAdapter([sys.executable, '-c', script])('Hallo'), struct.pack('<hh', 123, -456))

    def test_sync_timeout_busy_and_capacity_recovery(self):
        def blocked():
            import time
            time.sleep(30)
        async def run():
            first = asyncio.create_task(_call(blocked, timeout=.2))
            second = asyncio.create_task(_call(blocked, timeout=.2))
            await asyncio.sleep(.05)
            with self.assertRaises(AudioUnavailable):
                await _call(lambda: 'busy', timeout=1)
            for task in (first, second):
                with self.assertRaises(asyncio.TimeoutError):
                    await task
            self.assertEqual(await _call(lambda: 'recovered', timeout=1), 'recovered')
        asyncio.run(run())

    def test_whisper_configuration_spawn_and_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = WhisperLocal(tmp, language='de')
            self.assertEqual(adapter.model_path, tmp)
            self.assertNotIn('model', vars(adapter))
            class SlowWhisper(WhisperLocal):
                def __call__(self, pcm):
                    import time
                    time.sleep(30)
            slow = SlowWhisper(tmp, language='de')
            import multiprocessing
            original = multiprocessing.get_context
            contexts = []
            def observed(method=None):
                contexts.append(method)
                return original(method)
            async def run():
                with self.assertRaises(asyncio.TimeoutError):
                    await _call(slow, b'\0\0', timeout=.3)
                self.assertEqual(await _call(lambda: 'free', timeout=2), 'free')
            before = {p.pid for p in multiprocessing.active_children()}
            with patch('connector.multiprocessing.get_context', side_effect=observed):
                asyncio.run(run())
            self.assertEqual(contexts, ['spawn', 'spawn'])
            self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_command_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'descendant'
            script = ('import subprocess,time,sys; '
                      'subprocess.Popen([sys.executable,"-c",'
                      '"import time,pathlib; time.sleep(1); pathlib.Path(%r).write_text(\"alive\")"]); '
                      'time.sleep(30)') % str(marker)
            with self.assertRaises(AudioUnavailable):
                CommandAdapter([sys.executable, '-c', script], timeout=1)(b'')
            import time
            time.sleep(1.2)
            self.assertFalse(marker.exists())

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

    def test_second_turn_bounds_speech_and_logs_only_safe_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite')
            token=store.confirm(store.pair('owner','kai','orb'),'kai','orb')
            spoken=[]
            turns=iter(('Kurz.', 'Geheime Antwort ' * 15))
            async def synth(text):
                spoken.append(text)
                if text=='Kurz.': return b'\0\0'*100
                raise AudioUnavailable('private transcript https://secret.invalid/?key=hidden')
            async def turn(text,session): return next(turns)
            app=create_app(store,transcribe=lambda pcm:'private question',hermes_profile='kai',
                           synthesize=synth,turn_runner=turn)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
                    return [await c.post('/v1/turn-stream',content=b'\0\0'*100,
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'}) for _ in range(2)]
            with self.assertLogs('si_m_bridge.audio',level='WARNING') as logs:
                responses=asyncio.run(run())
            self.assertEqual([r.status_code for r in responses],[200,503])
            self.assertLessEqual(len(spoken[1]),80)
            self.assertIn('stage=tts kind=AudioUnavailable',logs.output[0])
            for forbidden in ('private question','Geheime','secret.invalid','hidden',token):
                self.assertNotIn(forbidden,logs.output[0])
            store.db.close()

    def test_long_reply_is_spoken_entirely_in_bounded_chunks(self):
        reply='Äpfel und Birnen. ' * 12
        spoken=[]
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite')
            token=store.confirm(store.pair('owner','kai','orb'),'kai','orb')
            async def synth(text):
                spoken.append(text)
                return b'\x01\0'*1600
            app=create_app(store,transcribe=lambda pcm:'question',hermes_profile='kai',
                           synthesize=synth,turn_runner=lambda text,session:reply)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
                    return await c.post('/v1/turn-stream',content=b'\0\0'*100,
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'})
            response=asyncio.run(run())
            self.assertEqual(response.status_code,200)
            self.assertEqual(' '.join(spoken),reply.strip())
            self.assertGreater(len(spoken),1)
            self.assertLessEqual(len(response.content),1_280_000+100)
            store.db.close()

if __name__=='__main__': unittest.main()
