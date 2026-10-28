"""Voice self-test: synthesize speech with Piper, round-trip it through ffmpeg like the browser does,
then transcribe it the same way the /api/voice/transcribe endpoint now does (numpy array, no PyAV).
Run inside the Pat container: /opt/pat-venv/bin/python /opt/pat/voice-selftest.py"""
import io, os, subprocess, tempfile, wave
import numpy as np

VOICE = os.environ.get("PAT_PIPER_VOICE", "en_US-amy-medium")
VDIR = os.environ.get("PAT_VOICE_DIR", "/opt/pat/voice")
PHRASE = "The quick brown fox jumps over the lazy dog."

try:
    from piper import PiperVoice
except ImportError:
    from piper.voice import PiperVoice

# 1) TTS -> wav
v = PiperVoice.load(f"{VDIR}/{VOICE}.onnx")
say = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
wf = wave.open(say, "wb"); first = True
for ch in v.synthesize(PHRASE):
    if first:
        wf.setnchannels(ch.sample_channels); wf.setsampwidth(ch.sample_width); wf.setframerate(ch.sample_rate); first = False
    wf.writeframes(ch.audio_int16_bytes)
wf.close()
print("TTS ok:", os.path.getsize(say), "bytes")

# 2) emulate the browser: wav -> webm/opus
webm = say + ".webm"
subprocess.run(["ffmpeg", "-y", "-i", say, "-c:a", "libopus", webm], capture_output=True, timeout=60)
print("webm ok:", os.path.getsize(webm), "bytes")

# 3) endpoint path: webm -> 16k mono wav -> numpy -> whisper
w16 = say + ".16k.wav"
subprocess.run(["ffmpeg", "-y", "-i", webm, "-ar", "16000", "-ac", "1", w16], capture_output=True, timeout=60)
with wave.open(w16, "rb") as w:
    pcm = w.readframes(w.getnframes())
audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
from faster_whisper import WhisperModel
m = WhisperModel(os.environ.get("PAT_WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
segs, _ = m.transcribe(audio, beam_size=1, language="en")
text = "".join(s.text for s in segs).strip()
print("TRANSCRIBED:", repr(text))
for p in (say, webm, w16):
    try: os.unlink(p)
    except OSError: pass
