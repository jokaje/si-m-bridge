"""Bounded local PCM adapters and KIM stream-v1 framing."""
import asyncio
import io
import os
import struct
import subprocess
import wave
from array import array

import httpx


class AudioUnavailable(Exception):
    pass


class CommandAdapter:
    """Explicit argv only: stdin PCM for STT, stdin UTF-8 for TTS; stdout text/WAV."""
    def __init__(self, argv, timeout=15, max_output=2_000_000):
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or '\x00' in x for x in argv):
            raise ValueError('adapter must be a nonempty argv list')
        if not 1 <= timeout <= 60 or not 1 <= max_output <= 2_000_000:
            raise ValueError('invalid adapter bounds')
        self.argv, self.timeout, self.max_output = argv, timeout, max_output

    def __call__(self, data):
        try:
            process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, shell=False, close_fds=True)
            try:
                output, _ = process.communicate(data, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
                raise AudioUnavailable('adapter timed out')
            if process.returncode or len(output) > self.max_output:
                raise AudioUnavailable('adapter failed or oversized')
            return output
        except OSError as exc:
            raise AudioUnavailable('adapter unavailable') from exc


class WhisperLocal:
    def __init__(self, model_path):
        if not model_path or not os.path.isdir(model_path):
            raise AudioUnavailable('local Whisper model directory required')
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_path, local_files_only=True, device='cpu', compute_type='int8')

    def __call__(self, pcm):
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        segments, _ = self.model.transcribe(samples, beam_size=1, vad_filter=False)
        return ' '.join(s.text.strip() for s in segments).strip()


def pcm_from_wav(data):
    if not isinstance(data, bytes) or len(data) > 2_000_000:
        raise AudioUnavailable('invalid WAV size')
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            rate, channels, width, count = wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()
            if rate not in (16000, 22050, 24000, 32000, 44100, 48000) or channels not in (1, 2) or width != 2 or count < 1 or count > rate * 20:
                raise AudioUnavailable('unsupported WAV format')
            raw = wav.readframes(count)
            if len(raw) != count * channels * 2: raise AudioUnavailable('truncated WAV')
    except (EOFError, wave.Error) as exc:
        raise AudioUnavailable('invalid WAV') from exc
    samples = array('h'); samples.frombytes(raw)
    if channels == 2: samples = array('h', ((int(samples[i]) + int(samples[i+1])) // 2 for i in range(0, len(samples), 2)))
    # Linear interpolation, bounded to 20 seconds; no external codecs or shell.
    out_count = len(samples) * 16000 // rate
    result = array('h', (int(samples[min(i * rate // 16000, len(samples)-1)]) if i * rate % 16000 == 0 else
                         int(samples[i * rate // 16000] + (samples[min(i * rate // 16000 + 1, len(samples)-1)] - samples[i * rate // 16000]) * (i * rate % 16000) / 16000)
                         for i in range(out_count)))
    if os.sys.byteorder != 'little': result.byteswap()
    return result.tobytes()


class LocalSpeechHTTP:
    def __init__(self, url):
        from connector import safe_url
        self.url = safe_url(url)

    async def __call__(self, text):
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=3), follow_redirects=False, trust_env=False) as client:
            async with client.stream('POST', self.url + '/v1/audio/speech',
                                     json={'model':'qwen3-tts','input':text,'response_format':'wav'}) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 2_000_000: raise AudioUnavailable('TTS response oversized')
        return pcm_from_wav(bytes(data))


def frame(kind, payload=b''):
    return kind.encode('ascii') + struct.pack('<I', len(payload)) + payload


def frames(pcm):
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 1_280_000:
        raise AudioUnavailable('invalid synthesized PCM')
    result = [frame('M', b'\x01')]
    chunk = 80_000
    for start in range(0, len(pcm), chunk): result.append(frame('A', pcm[start:start+chunk]))
    if len(result) > 17: raise AudioUnavailable('too many frames')
    result.append(frame('E'))
    return result
