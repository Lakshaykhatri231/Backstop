"""Which SSML controls does an Azure Dragon HD voice actually obey? Measured on the audio, not taken from the docs.

    PYTHONPATH=. python scripts/hd_voice_probe.py                       # the voice in AZURE_VOICE_EN (default en-IN-Meera HD)
    PYTHONPATH=. python scripts/hd_voice_probe.py en-US-Ava:DragonHDLatestNeural

A setting is "honoured" when it moves the audio well outside the voice's own run-to-run wobble (compare the two plain runs):
  pace / breaks -> the clip gets longer or shorter;  pitch -> the median fundamental frequency moves;
  styles -> a whisper gets much quieter, a shout higher and louder.
Calls the real Azure service with your key (a few dozen short requests; the key is never printed). macOS only (afconvert / afinfo).
Result on en-IN-Meera HD, 2026-10-23: pace and break honoured; pitch, express-as and inline [style] tags ignored. On en-US-Ava HD styles
are honoured. Microsoft's table lists prosody as unsupported for DragonHD, so re-run this if the voice or the service changes.
"""
import statistics
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import numpy as np

from app.config import settings

REGION = settings.azure_speech_region.strip().lower()
VOICE = sys.argv[1] if len(sys.argv) > 1 else settings.azure_voice_en
TEXT = "I understand, that can happen. Just tell me a date that works for you, there is no hurry at all."


def tts(inner: str) -> bytes:
    ssml = ("<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='en-IN'>"
            f"<voice name='{VOICE}'>{inner}</voice></speak>")
    r = httpx.post(f"https://{REGION}.tts.speech.microsoft.com/cognitiveservices/v1",
                   headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key, "Content-Type": "application/ssml+xml",
                            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3", "User-Agent": "hd-voice-probe"},
                   content=ssml.encode(), timeout=60)
    r.raise_for_status()
    return r.content


def features(mp3: bytes) -> tuple[float, float, float]:
    """(seconds, mean loudness of the voiced frames, median pitch in Hz)"""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.mp3").write_bytes(mp3)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@24000", f"{d}/a.mp3", f"{d}/a.wav"], check=True, capture_output=True)
        w = wave.open(f"{d}/a.wav")
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
    hop, win = int(sr * 0.01), int(sr * 0.04)
    rms, f0 = [], []
    for i in range(0, len(x) - win, hop):
        seg = x[i:i + win] - x[i:i + win].mean()
        r = float(np.sqrt((seg ** 2).mean()))
        if r < 300:
            continue
        rms.append(r)
        ac = np.correlate(seg, seg, "full")[win - 1:]
        lo, hi = int(sr / 400), int(sr / 70)
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / (ac[0] + 1e-9) > 0.45:
            f0.append(sr / k)
    return len(x) / sr, float(np.mean(rms)), float(np.median(f0)) if f0 else float("nan")


def run(label: str, inner: str, n: int = 3) -> None:
    rows = [features(tts(inner)) for _ in range(n)]
    m = lambda i: statistics.mean(r[i] for r in rows)
    print(f"  {label:46s} length {m(0):5.2f}s   loudness {m(1):6.0f}   pitch {m(2):4.0f} Hz")


e = escape(TEXT)
print("voice:", VOICE)
run("plain (run 1)", e)
run("plain (run 2: the voice's own run-to-run wobble)", e)
run("pace -40%", f"<prosody rate='-40%'>{e}</prosody>")
run("pace +40%", f"<prosody rate='+40%'>{e}</prosody>")
run("a 2 s break in the middle", escape("I understand, that can happen.") + "<break time='2000ms'/>" + escape("Just tell me a date that works for you."))
run("pitch +40%", f"<prosody pitch='+40%'>{e}</prosody>")
run("pitch -40%", f"<prosody pitch='-40%'>{e}</prosody>")
for style in ("whispering", "shouting"):
    run(f"express-as {style} (SSML)", f"<mstts:express-as style='{style}'>{e}</mstts:express-as>")
    run(f"[{style}] inline tag", f"[{style}] {e}")
