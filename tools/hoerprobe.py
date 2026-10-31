"""Erzeugt eine echte Hoerprobe der Audiokette (PCM -> STT -> Turn -> TTS -> PCM)."""
import asyncio
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))

import httpx
from audio_bridge import LocalSpeechHTTP, WhisperLocal
from connector import Store, create_app

MODEL = os.environ['SIM_TEST_STT_MODEL']
TTS = os.environ['SIM_TEST_TTS_URL']
LANG = os.environ.get('SIM_TEST_STT_LANGUAGE') or None
OUT = Path('/tmp/si-m-bridge-hoerprobe')

FRAGE = 'Wie ist das Wetter morgen in Hamburg?'
ANTWORT = 'Ja, morgen wird es sonnig.'


def frames(body):
    out, pos = [], 0
    while pos < len(body):
        kind = body[pos:pos + 1]
        length = int.from_bytes(body[pos + 1:pos + 5], 'little')
        out.append((kind, body[pos + 5:pos + 5 + length]))
        pos += 5 + length
    return out


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)


def main():
    stt = WhisperLocal(MODEL, language=LANG)
    tts = LocalSpeechHTTP(TTS)

    pcm_in = asyncio.run(tts(FRAGE))
    erkannt = stt(pcm_in)

    seen = {}

    def turn(text, session):
        seen['text'] = text
        return ANTWORT

    OUT.mkdir(parents=True, exist_ok=True)
    store = Store(OUT / 'store.db')
    try:
        code = store.pair('owner', 'orb', 'device')
        token = store.confirm(code, 'orb', 'device')
        app = create_app(store, transcribe=stt, synthesize=tts,
                         hermes_profile='orb', turn_runner=turn)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost', timeout=90) as client:
                return await client.post(
                    '/v1/turn-stream', content=pcm_in,
                    headers={'Authorization': 'Bearer ' + token,
                             'Content-Type': 'application/vnd.kim.pcm;rate=16000;channels=1;format=s16le'})

        response = asyncio.run(run())
        print('HTTP', response.status_code, '| Frames:', len(frames(response.content)))
        assert response.status_code == 200, response.text[:300]

        audio = b''.join(p for k, p in frames(response.content) if k == b'A')
        write_wav(OUT / 'frage.wav', pcm_in)
        write_wav(OUT / 'antwort.wav', audio)
        zurueck = stt(audio)

        print('Gesprochen   :', FRAGE)
        print('Erkannt (STT):', erkannt)
        print('Agent-Antwort:', ANTWORT)
        print('PCM zurueck  :', len(audio), 'Bytes = %.1f s' % (len(audio) / 32000))
        print('Gehoert (STT):', zurueck)
        print('Dateien      :', OUT / 'frage.wav', OUT / 'antwort.wav')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
