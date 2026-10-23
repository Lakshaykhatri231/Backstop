"""
Speech for the Demo Console: a voice to speak the call script and an ear to
hear the customer's reply.

This is a convenience layer for the browser simulator - it is NOT a telephony
integration, and it never decides anything. The script is still a fixed
template, and what the customer "meant" is still decided by voice.py's bounded
classifier. Speech only changes how text becomes sound and sound becomes text.

- Speaking: Azure AI Speech (neural Hindi / Indian-English voices). With no key
  the browser's own voice is used instead (the client falls back by itself).
- Listening: Groq-hosted Whisper. With no key the browser's own recogniser is
  used (Chrome only) or the reply is typed.
- Keys live only in `.env`; they go in request headers (httpx logs URLs, not
  headers), are never logged, and are never returned by any endpoint.
- Unknown provider values are refused rather than silently replaced.
"""
import array
import hashlib
import io
import logging
import re
import threading
import wave
from collections import OrderedDict
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TTS_CHARS = 1500
MAX_AUDIO_BYTES = 8 * 1024 * 1024
_REGION = re.compile(r"^[a-z0-9]{3,30}$")

# Whisper gets a short example in the style we expect so Hinglish comes back in
# Roman letters (what our keyword fallback reads best) rather than Devanagari.
_STT_PROMPT = {
    "hinglish": "Haan ji, main parso tak payment kar dunga. Abhi link bhej do.",
    "hi": "हाँ जी, मैं परसों तक पेमेंट कर दूँगा।",
    "en": "Yes, I will pay by Friday.",
}
_STT_LANG = {"hinglish": "hi", "hi": "hi", "en": "en"}


class SpeechError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def tts_provider() -> str:
    p = settings.voice_tts_provider
    if p == "auto":
        return "azure" if (settings.azure_speech_key and settings.azure_speech_region) else "browser"
    if p in ("azure", "browser"):
        return p
    raise SpeechError("provider_not_available", f"VOICE_TTS_PROVIDER '{p}' is not available.", 503)


def stt_provider() -> str:
    p = settings.voice_stt_provider
    if p == "auto":
        return "groq" if settings.groq_api_key else "browser"
    if p in ("groq", "browser"):
        return p
    raise SpeechError("provider_not_available", f"VOICE_STT_PROVIDER '{p}' is not available.", 503)


def config() -> dict:
    """What the browser may use. Never contains a key."""
    try:
        tts, stt = tts_provider(), stt_provider()
    except SpeechError as e:
        return {"tts": "browser", "stt": "browser", "error": e.message}
    return {"tts": tts, "stt": stt,
            "voices": ({"hi": settings.azure_voice_hi, "en": settings.azure_voice_en,
                        "hinglish": settings.azure_voice_hinglish} if tts == "azure" else {}),
            "stt_model": settings.groq_stt_model if stt == "groq" else None}


def is_hd(voice: str) -> bool:
    """Dragon HD voices: expressive by themselves (they read the MEANING of the text). Measured on this account's en-IN-Meera HD voice
    (2026-10-23, scripts/hd_voice_probe.py): pace (`prosody rate`) and `break` ARE honoured; pitch and speaking styles (`express-as`,
    inline [tags]) are NOT. Microsoft's own table lists `prosody` as unsupported for DragonHD, so the pace behaviour is undocumented and
    could change: `AZURE_HD_DELIVERY=false` turns it off. A plain-text voice with styles (en-US-Ava HD) was measured to honour them."""
    return "dragonhd" in (voice or "").lower()


def _voice_for(language: str) -> tuple[str, str]:
    name = {"hi": settings.azure_voice_hi, "en": settings.azure_voice_en}.get(
        language, settings.azure_voice_hinglish)
    # "hi-IN-SwaraNeural" -> "hi-IN"
    return name, "-".join(name.split("-")[:2])


# ── Delivery: pauses, pace and pitch ─────────────────────────────────────────
# A flat voice is mostly a flat DELIVERY: one even pace, no breaths between sentences, the
# same pitch from start to end. So each reply is spoken as separate pieces:
#   - a leading filler ("Acha,") is quicker and a touch lower, then a short breath;
#   - the first sentence (the acknowledgement) is a little livelier;
#   - middle sentences are steady; amounts and dates are slowed so they are easy to catch;
#   - a closing question rises slightly and is a touch slower;
#   - a breath (~300 ms) between sentences, a shorter one inside a long sentence.
# Everything is derived from the text alone, so the same line always sounds the same and the
# cache stays valid. Bump PROSODY_VERSION when this changes (it is part of the cache key).
PROSODY_VERSION = "v4"

# Per-customer delivery on the STANDARD voices (an HD voice ignores SSML; there the wording and punctuation carry it, see
# persona.py): (rate change in points, pitch change in points, breath length multiplier). A gentle approach is slower, a touch
# lower and takes longer breaths; a brief one is a little quicker and breathes less.
APPROACH_DELIVERY = {"gentle": (-6, -1, 1.3), "brief": (4, 0, 0.8), "steady": (-2, 0, 1.0)}

_FILLER = re.compile(
    r"^\s*(Hmm|Acha|Achha|Ji|Haan ji|Theek hai|Bilkul|Oh|Arre|Okay|Sure|Right|Of course|I see|"
    r"हम्म|अच्छा|जी|हाँ जी|ठीक है|बिल्कुल|ओह|अरे)\s*([,!.])\s*")
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")
_AMOUNT_OR_DATE = re.compile(r"(₹\s?[\d,]+(?:\.\d+)?|Rs\.?\s?[\d,]+|\b\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b)")
_BREATH_SENTENCE_MS, _BREATH_FILLER_MS, _BREATH_CLAUSE_MS, _BREATH_BEFORE_QUESTION_MS = 300, 200, 140, 380
_LONG_SENTENCE = 110


def _shift(pct: str, delta: int) -> str:
    return f"{int(pct.rstrip('%')) + delta:+d}%"


def _prosody(rate: str, pitch: str, inner: str, delivery: tuple[int, int, float] = (0, 0, 1.0)) -> str:
    return f"<prosody rate='{_shift(rate, delivery[0])}' pitch='{_shift(pitch, delivery[1])}'>{inner}</prosody>"


def _slow_numbers(escaped: str) -> str:
    return _AMOUNT_OR_DATE.sub(lambda m: f"<prosody rate='-10%'>{m.group(1)}</prosody>", escaped)


def _split_long(sentence: str) -> list[str]:
    """Cut a long sentence at the comma nearest its middle, so it can take a breath."""
    if len(sentence) <= _LONG_SENTENCE:
        return [sentence]
    mid = len(sentence) // 2
    commas = [m.start() for m in re.finditer(r",\s", sentence)]
    if not commas:
        return [sentence]
    cut = min(commas, key=lambda i: abs(i - mid))
    if cut < 25 or len(sentence) - cut < 25:
        return [sentence]
    return [sentence[: cut + 1].strip(), sentence[cut + 1 :].strip()]


# Dragon HD voices: the ONLY per-customer delivery they obey is pace and pauses (see is_hd). (rate change in points, extra breath between
# sentences in ms). Standard approach = untouched plain text, because the plain HD voice is the one that was judged most natural; only
# customers whose approach asks for a different manner get any change.
HD_DELIVERY = {"gentle": (-3, 100), "brief": (6, 0)}


def hd_delivery_ssml(text: str, approach: str | None = None) -> str:
    """The inside of a <voice> for a Dragon HD voice: plain text, or - for an approach with a manner of its own - the same text with
    a slower / quicker pace and a little extra breath after each sentence. No pitch or style (the voice ignores both)."""
    text = (text or "").strip()
    d = HD_DELIVERY.get(approach or "")
    if d is None or not settings.azure_hd_delivery:
        return escape(text)
    rate, pause = d
    sentences = [x for x in _SENTENCE_END.split(text) if x.strip()]
    parts = []
    for i, sent in enumerate(sentences):
        if i > 0 and pause:
            parts.append(f"<break time='{pause}ms'/>")
        parts.append(f"<prosody rate='{rate:+d}%'>{escape(sent)}</prosody>")
    return "".join(parts) or escape(text)


def delivery_ssml(text: str, approach: str | None = None) -> str:
    """The inside of a <voice>: the text split into pieces with their own pace, pitch and breaths."""
    text = (text or "").strip()
    d = APPROACH_DELIVERY.get(approach or "", (0, 0, 1.0))
    ms = lambda n: int(n * d[2])
    parts: list[str] = []
    m = _FILLER.match(text)
    if m:
        parts.append(_prosody("-8%", "-2%", escape(m.group(1) + m.group(2)), d))
        parts.append(f"<break time='{ms(_BREATH_FILLER_MS)}ms'/>")
        text = text[m.end():]
    sentences = [x for x in _SENTENCE_END.split(text) if x.strip()]
    for i, sent in enumerate(sentences):
        is_q = sent.rstrip().endswith(("?", "？"))
        is_last = i == len(sentences) - 1
        if i > 0:
            parts.append(f"<break time='{ms(_BREATH_BEFORE_QUESTION_MS if is_q and is_last else _BREATH_SENTENCE_MS)}ms'/>")
        if is_q and is_last:
            rate, pitch = "-6%", "+3%"          # a question that invites an answer: unhurried, rising a little
        elif i == 0:
            rate, pitch = "-2%", "+1%"          # the acknowledgement: a little livelier
        else:
            rate, pitch = "-4%", "+0%"          # the steady middle
        chunks = _split_long(sent)
        for j, chunk in enumerate(chunks):
            if j > 0:
                parts.append(f"<break time='{ms(_BREATH_CLAUSE_MS)}ms'/>")
            parts.append(_prosody(rate, pitch, _slow_numbers(escape(chunk)), d))
    return "".join(parts) or escape(text)


# Azure speaking styles exist only on some voices (probed from the voices/list
# endpoint for this account: Neerja and Swara have "empathetic" and "cheerful").
_STYLE_VOICES = {"en-IN-NeerjaNeural", "hi-IN-SwaraNeural"}

_mem: "OrderedDict[str, bytes]" = OrderedDict()
_mem_lock = threading.Lock()
_MEM_MAX = 200


def _cache_key(voice: str, style: str | None, degree: float | None, text: str, approach: str | None = None) -> str:
    return hashlib.sha256(
        f"{PROSODY_VERSION}|{voice}|{style or ''}|{degree or ''}|{approach or ''}|{text}".encode("utf-8")).hexdigest()


def synthesize_cached(text: str, language: str, style: str | None = None,
                      degree: float | None = None, approach: str | None = None) -> tuple[bytes, bool]:
    """(mp3 bytes, cache_hit). The same line in the same voice is only ever paid
    for once - it saves the free Azure allowance and the round trip (replays,
    "one moment" clips, the fixed answers every customer hears)."""
    voice, _ = _voice_for(language)
    key = _cache_key(voice, style, degree, (text or "").strip(), approach)
    with _mem_lock:
        hit = _mem.get(key)
        if hit is not None:
            _mem.move_to_end(key)
            return hit, True
    path = Path(settings.tts_cache_dir) / f"{key}.mp3"
    try:
        if path.exists():
            data = path.read_bytes()
            with _mem_lock:
                _mem[key] = data
                while len(_mem) > _MEM_MAX:
                    _mem.popitem(last=False)
            return data, True
    except OSError:
        pass
    try:
        data = synthesize(text, language, style, degree, approach=approach)
    except SpeechError as e:
        if not (is_hd(voice) and e.code in ("tts_quota", "tts_failed", "tts_auth")):
            raise
        # The HD voice was refused (not in the free allowance / over quota): speak with the standard
        # voice instead so the call never goes silent. Not cached, so the HD voice is retried next time.
        logger.warning("HD voice unavailable (%s); using %s", e.code, settings.azure_voice_fallback)
        return synthesize(text, language, style, degree, voice_override=settings.azure_voice_fallback, approach=approach), False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        logger.info("tts cache dir not writable; continuing without disk cache")
    with _mem_lock:
        _mem[key] = data
        while len(_mem) > _MEM_MAX:
            _mem.popitem(last=False)
    return data, False


def synthesize(text: str, language: str, style: str | None = None, degree: float | None = None,
               voice_override: str | None = None, approach: str | None = None) -> bytes:
    """Text -> MP3 bytes via Azure AI Speech."""
    if tts_provider() != "azure":
        raise SpeechError("tts_not_configured", "Cloud voice is not configured; the browser voice is used.", 503)
    region = settings.azure_speech_region.strip().lower()
    if not _REGION.match(region):
        raise SpeechError("bad_region", "AZURE_SPEECH_REGION looks wrong (expected e.g. centralindia).", 503)
    text = (text or "").strip()
    if not text:
        raise SpeechError("empty_text", "Nothing to speak.", 422)
    voice, xml_lang = _voice_for(language)
    if voice_override:
        voice, xml_lang = voice_override, "-".join(voice_override.split("-")[:2])
    if is_hd(voice):
        # An HD voice reads the text the way a person would, driven by wording and punctuation. It gets plain text, except that a
        # gentle / brief approach adds pace and pauses (the only things it obeys). No pitch, no style: measured to do nothing here.
        body = hd_delivery_ssml(text[:MAX_TTS_CHARS], approach)
    else:
        body = delivery_ssml(text[:MAX_TTS_CHARS], approach)
    if style and voice in _STYLE_VOICES:
        deg = f" styledegree='{degree}'" if degree else ""
        body = f"<mstts:express-as style='{style}'{deg}>{body}</mstts:express-as>"
    ssml = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            f"xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='{xml_lang}'>"
            f"<voice name='{voice}'>{body}</voice></speak>")
    try:
        r = httpx.post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={"Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                     "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                     "User-Agent": "revenue-recovery-demo"},
            content=ssml.encode("utf-8"), timeout=20)
    except httpx.HTTPError as e:
        logger.warning("azure tts request failed: %s", type(e).__name__)
        raise SpeechError("tts_unreachable", "Could not reach the voice service.")
    if r.status_code in (401, 403):
        raise SpeechError("tts_auth", "Azure rejected the key or region. Check AZURE_SPEECH_KEY / AZURE_SPEECH_REGION.")
    if r.status_code == 429:
        raise SpeechError("tts_quota", "The free voice allowance for this month looks used up.", 429)
    if r.status_code != 200 or not r.content:
        logger.warning("azure tts status %s", r.status_code)
        raise SpeechError("tts_failed", f"Voice service answered {r.status_code}.")
    return r.content


# Whisper is known to invent a stock phrase on near-silence. Only trusted
# when the clip is also very short.
_PHANTOMS = {"thank you", "thanks", "thank you for watching", "thanks for watching", "bye", "you",
             "subtitles by", "please subscribe", "धन्यवाद", "शुक्रिया", "thank you."}
MIN_CLIP_S = 0.3          # a quick "haan" or "nahh" is often 0.3-0.4 s of speech
MIN_RMS = 0.004          # of full scale; below this the clip is room noise


def inspect_wav(audio: bytes) -> dict | None:
    """Duration and loudness of a 16-bit PCM WAV; None if it is not one."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() != 2:
                return None
            n, rate, ch = w.getnframes(), w.getframerate(), w.getnchannels()
            raw = w.readframes(n)
    except (wave.Error, EOFError):
        return None
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return {"duration_s": 0.0, "rms": 0.0}
    step = max(1, len(samples) // 20000)
    sub = samples[::step]
    rms = (sum(x * x for x in sub) / len(sub)) ** 0.5 / 32768.0
    return {"duration_s": n / float(rate or 1), "rms": rms}


def transcribe_clip(audio: bytes, content_type: str, language: str, bias: str = "") -> dict:
    """{text, reason?, duration_ms?}. A clip that is too short, silent or a known
    phantom is answered with empty text and a reason - never an error - so the
    live call simply keeps listening."""
    info = inspect_wav(audio)
    out: dict = {"text": ""}
    if info is not None:
        out["duration_ms"] = int(info["duration_s"] * 1000)
        if info["duration_s"] < MIN_CLIP_S:
            return {**out, "reason": "too_short"}
        if info["rms"] < MIN_RMS:
            return {**out, "reason": "silent"}
    text = transcribe(audio, content_type, language, bias)
    norm = re.sub(r"[^\w\u0900-\u097f ]", "", text.lower()).strip()
    if text and info is not None and info["duration_s"] < 1.2 and norm in {re.sub(r"[^\w\u0900-\u097f ]", "", p) for p in _PHANTOMS}:
        return {**out, "reason": "phantom"}
    return {**out, "text": text}


def transcribe(audio: bytes, content_type: str, language: str, bias: str = "") -> str:
    """Audio -> text via Groq-hosted Whisper. `bias` (merchant name, invoice ref)
    goes into the prompt so names and numbers are spelled the way we know them."""
    if stt_provider() != "groq":
        raise SpeechError("stt_not_configured", "Cloud listening is not configured.", 503)
    if not audio:
        raise SpeechError("empty_audio", "No audio received.", 422)
    if len(audio) > MAX_AUDIO_BYTES:
        raise SpeechError("audio_too_large", "That recording is too long.", 413)
    ext = "webm"
    ct = (content_type or "").lower()
    if "mp4" in ct or "m4a" in ct:
        ext = "mp4"
    elif "ogg" in ct:
        ext = "ogg"
    elif "wav" in ct:
        ext = "wav"
    elif "mpeg" in ct or "mp3" in ct:
        ext = "mp3"
    lang = language if language in _STT_LANG else "hinglish"
    try:
        r = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            files={"file": (f"reply.{ext}", audio, ct or "audio/webm")},
            data={"model": settings.groq_stt_model,
                  **({"language": _STT_LANG[lang]} if settings.stt_language_mode != "auto" else {}),
                  "prompt": (_STT_PROMPT[lang] + (" " + bias if bias else ""))[:600],
                  "response_format": "json", "temperature": "0"},
            timeout=30)
    except httpx.HTTPError as e:
        logger.warning("groq stt request failed: %s", type(e).__name__)
        raise SpeechError("stt_unreachable", "Could not reach the listening service.")
    if r.status_code in (401, 403):
        raise SpeechError("stt_auth", "Groq rejected the key.")
    if r.status_code == 429:
        raise SpeechError("stt_quota", "Groq's free listening limit was reached; try again shortly.", 429)
    if r.status_code != 200:
        logger.warning("groq stt status %s", r.status_code)
        raise SpeechError("stt_failed", f"Listening service answered {r.status_code}.")
    try:
        return (r.json().get("text") or "").strip()
    except ValueError:
        raise SpeechError("stt_failed", "Listening service sent an unreadable answer.")
