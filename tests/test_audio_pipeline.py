"""End-to-end audio pipeline against real local components.

This is the test the earlier review wanted: real microphone-shaped PCM in, real
speech recognition, a real agent turn, real speech synthesis, real framed PCM
out. Nothing here is mocked except the agent itself, because calling a paying
model provider from a test suite is not acceptable.

Skipped unless the local pieces exist:

    SIM_TEST_STT_MODEL   local faster-whisper model directory
    SIM_TEST_TTS_URL     local OpenAI-compatible /v1/audio/speech endpoint

Both are opt-in on purpose: a fresh clone has neither.
"""
import asyncio
import os
import unittest
from array import array
from pathlib import Path

import httpx

from audio_bridge import AudioUnavailable, LocalSpeechHTTP, WhisperLocal, pcm_from_wav
from connector import Store, create_app

STT_MODEL = os.environ.get('SIM_TEST_STT_MODEL')
TTS_URL = os.environ.get('SIM_TEST_TTS_URL')
PIPELINE_READY = bool(STT_MODEL and Path(STT_MODEL).is_dir() and TTS_URL)

QUESTION = 'Wie ist das Wetter morgen in Hamburg?'
# Short and phonetically plain: speech recognition on a CPU model varies in
# wording ("sonnig" was heard as "so nicht" once), so the assertions below check
# audibility and word overlap rather than an exact transcript.
ANSWER = 'Ja, morgen wird es sonnig.'


def _frames(body):
    """Parse the M/A/E frame stream exactly as the firmware does."""
    out, pos = [], 0
    while pos < len(body):
        kind = body[pos:pos + 1]
        if len(kind) < 1:
            raise AssertionError('truncated frame header')
        length = int.from_bytes(body[pos + 1:pos + 5], 'little')
        payload = body[pos + 5:pos + 5 + length]
        if len(payload) != length:
            raise AssertionError('truncated payload')
        out.append((kind, payload))
        pos += 5 + length
    return out


@unittest.skipUnless(PIPELINE_READY,
                     'lokale Audiokette fehlt (SIM_TEST_STT_MODEL / SIM_TEST_TTS_URL setzen)')
class LiveAudioPipeline(unittest.TestCase):
    """One real round trip: PCM -> Whisper -> turn -> TTS -> framed PCM."""

    @classmethod
    def setUpClass(cls):
        cls.transcribe = WhisperLocal(STT_MODEL, language=os.environ.get('SIM_TEST_STT_LANGUAGE') or None)
        cls.synthesize = LocalSpeechHTTP(TTS_URL)

    def _make_utterance(self, text):
        """Ask the local TTS server for speech, hand it back as 16 kHz PCM."""
        return asyncio.run(self.synthesize(text))

    def test_full_round_trip_produces_speaking_frames(self):
        pcm_in = self._make_utterance(QUESTION)
        self.assertGreater(len(pcm_in), 16000, 'Sprachprobe zu kurz')

        heard = self.transcribe(pcm_in)
        self.assertTrue(heard.strip(), 'Spracherkennung lieferte nichts')

        seen = {}

        def turn(text, session):
            seen['text'], seen['session'] = text, session
            return ANSWER

        tmp = Path(os.environ.get('TMPDIR', '/tmp')) / ('pipeline-%d' % os.getpid())
        tmp.mkdir(parents=True, exist_ok=True)
        store = Store(tmp / 'store.db')
        try:
            code = store.pair('owner', 'orb', 'device')
            token = store.confirm(code, 'orb', 'device')
            app = create_app(store, transcribe=self.transcribe, synthesize=self.synthesize,
                             hermes_profile='orb', turn_runner=turn)

            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='http://localhost', timeout=60) as client:
                    return await client.post(
                        '/v1/turn-stream',
                        content=pcm_in,
                        headers={'Authorization': 'Bearer ' + token,
                                 'Content-Type': 'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'})

            response = asyncio.run(run())
            self.assertEqual(response.status_code, 200, response.text[:200])
            self.assertTrue(response.headers['content-type'].startswith('application/vnd.kim.stream-v1'))

            frames = _frames(response.content)
            self.assertEqual(frames[0][0], b'M')
            self.assertEqual(frames[0][1], b'\x01')
            self.assertEqual(frames[-1][0], b'E')
            self.assertEqual(frames[-1][1], b'')
            audio = b''.join(payload for kind, payload in frames if kind == b'A')
            self.assertGreater(len(audio), 0, 'keine Audiodaten in den A-Frames')
            self.assertEqual(len(audio) % 2, 0)

            # The turn saw the recognised speech, and the answer is audible.
            self.assertIn('wetter', seen['text'].lower())
            self.assertTrue(seen['session'].startswith('sim-'))

            spoken_back = self.transcribe(audio)
            self.assertTrue(spoken_back.strip(), 'Sprachausgabe war nicht vernehmbar')
            self.assertGreaterEqual(len(spoken_back.split()), 3, spoken_back)
            heard_words = {w.strip('.,!?').lower() for w in spoken_back.split()}
            wanted_words = {w.strip('.,!?').lower() for w in ANSWER.split()}
            overlap = heard_words & wanted_words
            self.assertGreaterEqual(len(overlap), 2,
                                    'Sprachausgabe nicht wiedererkennbar: %r (Treffer: %s)'
                                    % (spoken_back, sorted(overlap)))
        finally:
            store.db.close()

    def test_frames_are_bounded_and_ordered(self):
        pcm = self._make_utterance('Kurzer Test.')
        seen = {}
        tmp = Path(os.environ.get('TMPDIR', '/tmp')) / ('frames-%d' % os.getpid())
        tmp.mkdir(parents=True, exist_ok=True)
        store = Store(tmp / 'store.db')
        try:
            code = store.pair('owner', 'orb', 'device')
            token = store.confirm(code, 'orb', 'device')
            app = create_app(store, transcribe=self.transcribe, synthesize=self.synthesize,
                             hermes_profile='orb',
                             turn_runner=lambda text, session: ANSWER)
            received = {}

            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='http://localhost', timeout=60) as client:
                    return await client.post(
                        '/v1/turn-stream', content=pcm,
                        headers={'Authorization': 'Bearer ' + token,
                                 'Content-Type': 'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'})
            response = asyncio.run(run())
            frames = _frames(response.content)
            kinds = [kind for kind, _ in frames]
            self.assertEqual(kinds[0], b'M')
            self.assertEqual(kinds[-1], b'E')
            self.assertLessEqual(len(frames), 18, 'zu viele Frames')
            self.assertGreaterEqual(len(frames), 3)
            for kind, payload in frames[1:-1]:
                self.assertEqual(kind, b'A')
                self.assertGreater(len(payload), 0)
                self.assertLessEqual(len(payload), 256000)
                self.assertEqual(len(payload) % 2, 0)
        finally:
            store.db.close()


class LanguageHintTest(unittest.TestCase):
    """A language hint must be validated before a model is ever loaded."""

    def test_two_letter_code_accepted_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Invalid hint fails fast, before any model import or load.
            with self.assertRaises(AudioUnavailable):
                WhisperLocal(tmp, language='deutsch')
            with self.assertRaises(AudioUnavailable):
                WhisperLocal(tmp, language='D')

    def test_missing_model_directory_rejected(self):
        with self.assertRaises(AudioUnavailable):
            WhisperLocal('/pfad/gibt/es/nicht')


if __name__ == '__main__':
    unittest.main()
