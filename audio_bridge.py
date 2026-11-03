"""Bounded local PCM adapters and KIM stream-v1 framing."""
import asyncio
import io
import os
import signal
import re
import struct
import subprocess
import wave
from array import array

import httpx


class AudioUnavailable(Exception):
    pass


def speech_chunks(text, max_chars=75, max_chunks=12):
    """Split complete speech at word boundaries; reject impossible wire budgets."""
    if not isinstance(text, str) or not text.strip():
        raise AudioUnavailable('empty speech')
    words = text.split()
    chunks = []
    current = ''
    for word in words:
        if len(word) > max_chars:
            raise AudioUnavailable('speech word exceeds chunk limit')
        candidate = (current + ' ' + word).strip()
        if len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > max_chunks:
        raise AudioUnavailable('speech exceeds turn budget')
    return chunks


class CommandAdapter:
    """Explicit argv only: stdin PCM for STT, stdin UTF-8 for TTS; stdout text/WAV."""
    def __init__(self, argv, timeout=15, max_output=2_000_000):
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or '\x00' in x for x in argv):
            raise ValueError('adapter must be a nonempty argv list')
        if not 1 <= timeout <= 60 or not 1 <= max_output <= 2_000_000:
            raise ValueError('invalid adapter bounds')
        self.argv, self.timeout, self.max_output = argv, timeout, max_output

    def __call__(self, data):
        is_tts = isinstance(data, str)
        if is_tts:
            input_bytes = data.encode('utf-8')
        elif isinstance(data, (bytes, bytearray)):
            input_bytes = bytes(data)
        else:
            raise AudioUnavailable('invalid adapter input type')
        try:
            process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                                       start_new_session=True)
            try:
                output, _ = process.communicate(input_bytes, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()
                raise AudioUnavailable('adapter timed out')
            if process.returncode or len(output) > self.max_output:
                raise AudioUnavailable('adapter failed or oversized')
            if is_tts and output.startswith(b'RIFF') and output[8:12] == b'WAVE':
                return pcm_from_wav(output)
            return output
        except OSError as exc:
            raise AudioUnavailable('adapter unavailable') from exc


class WhisperLocal:
    """Local faster-whisper model.

    `language` is an optional ISO-639-1 hint. Without it the model guesses, which
    on small models regularly produces the wrong language entirely (a German
    sentence came back as Japanese with `base`). Pass the language the user
    picked in the app here instead of letting the model decide.
    """
    def __init__(self, model_path, language=None):
        if not model_path or not os.path.isdir(model_path):
            raise AudioUnavailable('local Whisper model directory required')
        if language is not None and (not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}', language)):
            raise AudioUnavailable('language hint must be a two-letter code')
        # Keep only spawn-serializable configuration in the web process. Native
        # CTranslate2 threads and model weights must never be inherited by fork.
        self.model_path = model_path
        self.language = language

    def __call__(self, pcm):
        from faster_whisper import WhisperModel
        import numpy as np
        model = WhisperModel(self.model_path, local_files_only=True, device='cpu', compute_type='int8')
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        segments, _ = model.transcribe(samples, beam_size=1, vad_filter=False,
                                            language=self.language)
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
