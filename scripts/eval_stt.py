"""
Compare listening setups on what actually matters: does the right INTENT (and
promise date) come out of what the customer said?

  python scripts/eval_stt.py                  # built-in sentences, spoken by macOS `say`
  python scripts/eval_stt.py --dir my_clips   # your own recordings + my_clips/labels.csv
                                              #   columns: file,language,expected_intent,expected_days
                                              #   (language hi|hinglish|en; expected_days blank if no date)

Setups tried: Groq Whisper with the call's language fixed, Whisper with auto language
detection, and (when AZURE_SPEECH_KEY is set) Azure en-IN and hi-IN. Whisper calls are
spaced out to stay inside Groq's free per-minute limit. Nothing here changes the app;
use the result to choose STT_LANGUAGE_MODE.
"""
import argparse
import csv
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import speech, voice, clock                       # noqa: E402
from app.config import settings                             # noqa: E402

BUILTIN = [   # (text, voice, language, expected_intent, expected_days)
    ("Haan ji, main parso tak payment kar dunga", "Rishi", "hinglish", "PROMISE", 2),
    ("Kal tak ho jayega, pakka", "Rishi", "hinglish", "PROMISE", 1),
    ("Main paanch din baad payment kar dunga", "Rishi", "hinglish", "PROMISE", 5),
    ("Abhi link bhej do, main payment kar deta hoon", "Rishi", "hinglish", "PAY_NOW", None),
    ("Yeh bill galat hai, maal damaged mila tha", "Rishi", "hinglish", "DISPUTE", None),
    ("Aap galat number pe call kar rahe ho", "Rishi", "hinglish", "WRONG_NUMBER", None),
    ("Abhi busy hoon, thodi der baad call kariye", "Rishi", "hinglish", "CALLBACK", None),
    ("Nahi, hum payment nahi karenge, dobara call mat karna", "Rishi", "hinglish", "REFUSED", None),
    ("I will pay by day after tomorrow", "Rishi", "en", "PROMISE", 2),
    ("Please send me the payment link now, I will pay", "Rishi", "en", "PAY_NOW", None),
    ("हाँ जी, मैं परसों तक पेमेंट कर दूँगा", "Lekha", "hi", "PROMISE", 2),
    ("यह बिल गलत है", "Lekha", "hi", "DISPUTE", None),
    ("अभी लिंक भेज दो, मैं पे कर देता हूँ", "Lekha", "hi", "PAY_NOW", None),
]


def make_wav(text: str, say_voice: str, path: Path) -> None:
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", say_voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(path)], check=True)


def whisper(path: Path, language: str, mode: str) -> str:
    settings.stt_language_mode = mode
    return speech.transcribe(path.read_bytes(), "audio/wav", language)


def azure(path: Path, locale: str) -> str:
    r = httpx.post(
        f"https://{settings.azure_speech_region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
        params={"language": locale}, timeout=30,
        headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"},
        content=path.read_bytes())
    return (r.json().get("DisplayText") or "") if r.status_code == 200 else f"<HTTP {r.status_code}>"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder with your own WAVs and labels.csv")
    args = ap.parse_args()
    today = clock.now().date()
    tmp = Path(tempfile.mkdtemp(prefix="eval_stt_"))
    cases = []
    if args.dir:
        d = Path(args.dir)
        for row in csv.DictReader(open(d / "labels.csv", encoding="utf-8")):
            days = int(row["expected_days"]) if row.get("expected_days") else None
            cases.append((d / row["file"], row["file"], row["language"], row["expected_intent"], days))
    else:
        if subprocess.run(["which", "say"], capture_output=True).returncode != 0:
            sys.exit("The built-in samples need macOS `say`; use --dir with your own recordings.")
        for i, (text, vv, lang, intent, days) in enumerate(BUILTIN):
            p = tmp / f"s{i}.wav"
            make_wav(text, vv, p)
            cases.append((p, text, lang, intent, days))

    setups = [("whisper fixed", lambda p, l: whisper(p, l, "fixed")), ("whisper auto", lambda p, l: whisper(p, l, "auto"))]
    if settings.azure_speech_key and settings.azure_speech_region:
        setups += [("azure en-IN", lambda p, l: azure(p, "en-IN")), ("azure hi-IN", lambda p, l: azure(p, "hi-IN"))]

    score = {name: [0, 0] for name, _ in setups}       # [intent ok, date ok]
    dated = sum(1 for c in cases if c[4] is not None)
    for path, label, lang, intent, days in cases:
        print(f"\n[{lang}] {label}   -> expect {intent}" + (f" +{days}d" if days is not None else ""))
        for name, fn in setups:
            try:
                heard = fn(path, lang)
            except Exception as e:                         # network / rate limit
                heard = f"<{type(e).__name__}>"
            got = voice.keyword_classify(heard, today)
            ok = got["intent"] == intent
            dok = days is not None and got["promise_date"] == today + timedelta(days=days)
            score[name][0] += ok
            score[name][1] += bool(dok)
            print(f"   {name:14} {'OK ' if ok else 'BAD'} {got['intent']:12} {heard!r}")
            if name.startswith("whisper"):
                time.sleep(3.3)                            # Groq free tier: ~20 requests/minute
    print("\n── summary (keyword classifier on the transcript) ──")
    for name, (i_ok, d_ok) in score.items():
        print(f"{name:14} intent {i_ok}/{len(cases)}   date {d_ok}/{dated}")
    print("\nSet STT_LANGUAGE_MODE=auto only if 'whisper auto' beats 'whisper fixed' on your own clips.")


if __name__ == "__main__":
    main()
