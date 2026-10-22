"""Short spoken replies: "haan", "nahh", "hmm", "ek minute", "kya?".

On a phone line most answers are one to four words, and a model (or a keyword list built for sentences) reads them badly: a bare
"haan" came back UNCLEAR, "nahh" matched nothing. This module reads a WHOLE short utterance against fixed word lists, in Roman
Hinglish, English and Devanagari, tolerating stretched spellings ("nahhh", "haaan", "okkk"). It decides only WHAT KIND of reply it
was; what that means for the debt (a "haan" to "shall I send the link?" vs "by when?") is decided by the caller, who knows what the
agent just asked.

A line counts only when EVERY word is a known short phrase (so "nahi, kal kar dunga" is a sentence, not a "no", and goes to the normal
reader). Mixed meanings ("haan nahi") are refused, never guessed. "acha"/"hmm" are acknowledgements, not agreement - Hindi speakers
use them to show they are listening (Cambridge Dictionary: "accha" - agree with, or understand, something).
"""
from __future__ import annotations

import re

YES, NO, SEND, LATER, HOLD, REPEAT, ACK, UNSURE = "YES", "NO", "SEND", "LATER", "HOLD", "REPEAT", "ACK", "UNSURE"
MAX_WORDS = 7

_PHRASES: dict[str, tuple[str, ...]] = {
    YES: ("haan", "han", "haa", "ha", "hanji", "haanji", "ya", "yes", "yeah", "yep", "yup", "yea", "ok", "okay", "kk", "k",
          "theek hai", "theek", "thik hai", "thik", "tik hai", "sahi", "sahi hai", "bilkul", "zaroor", "jarur", "pakka", "done",
          "sure", "of course", "absolutely", "correct", "right", "fine", "alright", "all right", "sounds good", "chalega",
          "chalo", "chal", "kar do", "kar dijiye", "kar lo", "kar dena", "note kar lo", "note kar lijiye", "note kar do",
          "yes please", "haan please", "ho jayega", "ho jaega", "ho jayegi", "bilkul theek", "haan theek hai"),
    SEND: ("bhej do", "bhej dijiye", "bhejo", "bhej dena", "send it", "send", "send link", "send the link", "link bhejo",
           "link bhej do", "go ahead", "haan bhej do", "haan bhejo"),
    NO: ("nahi", "nahin", "nhi", "nai", "na", "nah", "no", "nope", "never", "mat", "mat karo", "nahi karna", "nahi chahiye",
         "rehne do", "rahne do", "no thanks", "no thank you", "not interested", "bilkul nahi", "nahi dena", "nahi karunga",
         "nahi karungi", "mujhe nahi chahiye"),
    LATER: ("abhi nahi", "baad mein", "baad me", "later", "not now", "busy", "busy hoon", "busy hun", "busy hu", "abhi busy",
            "main busy hoon", "mai busy hoon", "mai busy hu", "thodi der baad", "thodi der mein", "call later",
            "baad mein baat", "baad mein call karna", "baad mein karte hain", "baad mein karna"),
    HOLD: ("ruko", "ruk", "ruk jao", "ruko zara", "zara ruko", "thoda ruko", "thodi der ruko", "abhi ruko", "ek minute",
           "ek min", "ek second", "ek sec", "ek minute ruko", "ruko ek minute", "one minute", "one min", "one sec",
           "one second", "hold on", "hold", "wait", "wait a minute", "wait a sec", "just a minute", "just a moment",
           "a moment", "moment", "thehro", "give me a minute", "give me a moment", "give me a sec"),
    REPEAT: ("kya", "kya kaha", "kya bola", "kya bol rahe the", "what", "what did you say", "sorry", "pardon", "pardon me",
             "excuse me", "huh", "hain", "hein", "phir se", "dobara", "ek baar aur", "again", "repeat", "say again",
             "say that again", "once more", "samajh nahi aaya", "samjha nahi", "samajh nahi aya", "sunai nahi diya",
             "suna nahi", "awaaz nahi aa rahi", "awaz nahi aa rahi", "louder", "slowly", "dheere bolo", "dheere",
             "phir se bolo", "dobara bolo", "kya matlab"),
    UNSURE: ("pata nahi", "maloom nahi", "malum nahi", "dekhta hoon", "dekhti hoon", "dekhte hain", "dekhta hu",
             "soch ke batata hoon", "soch ke batati hoon", "sochta hoon", "sochti hoon", "maybe", "shayad", "not sure",
             "dont know", "i dont know", "let me see", "will see", "we will see", "kuch keh nahi sakta"),
    ACK: ("acha", "accha", "achha", "achcha", "hmm", "hm", "mm", "mhm", "uh huh", "oh", "oho", "ohh", "arre", "are", "i see",
          "ji", "jee", "hello", "hallo", "hi", "hey", "namaste", "namaskar", "boliye", "bolo", "bol", "speaking",
          "ji boliye", "haan boliye", "haan bolo", "yes speaking", "haan bol raha hoon", "haan main bol raha hoon",
          "thank you", "thanks", "thankyou", "dhanyavad", "shukriya"),
}

# Whole Devanagari words (and the common nukta spelling) -> the Roman word the lists above use.
_DEVA = {
    "हाँ": "haan", "हां": "haan", "हा": "haan", "हाँजी": "haan ji", "हांजी": "haan ji", "जी": "ji", "ठीक": "theek",
    "है": "hai", "नहीं": "nahi", "नही": "nahi", "नहिं": "nahi", "नहि": "nahi", "ना": "na", "मत": "mat",
    "रुको": "ruko", "रुकिए": "ruko", "रुकिये": "ruko", "रुक": "ruk", "एक": "ek", "मिनट": "minute", "सेकंड": "second",
    "सेकण्ड": "second", "क्या": "kya", "कहा": "kaha", "बोला": "bola", "अच्छा": "acha", "अच्छी": "acha", "हम्म": "hmm",
    "ओके": "ok", "यस": "yes", "बिल्कुल": "bilkul", "पक्का": "pakka", "जरूर": "zaroor", "ज़रूर": "zaroor", "बाद": "baad",
    "में": "mein", "अभी": "abhi", "भेज": "bhej", "दो": "do", "दीजिए": "dijiye", "दीजिये": "dijiye", "कर": "kar",
    "लो": "lo", "बोलिए": "boliye", "बोलिये": "boliye", "बोलो": "bolo", "हैलो": "hello", "हेलो": "hello",
    "नमस्ते": "namaste", "व्यस्त": "busy", "फिर": "phir", "से": "se", "दोबारा": "dobara", "समझ": "samajh",
    "आया": "aaya", "सॉरी": "sorry", "पता": "pata", "मालूम": "maloom", "देखता": "dekhta", "देखती": "dekhti",
    "हूँ": "hoon", "हूं": "hoon", "सही": "sahi", "चलेगा": "chalega", "नोट": "note", "धन्यवाद": "dhanyavad",
    "शुक्रिया": "shukriya", "थोड़ी": "thodi", "थोड़ा": "thoda", "देर": "der", "बात": "baat", "करना": "karna",
    "चाहिए": "chahiye", "रहने": "rehne", "सुनाई": "sunai", "नहीं": "nahi", "दिया": "diya", "ज़रा": "zara", "जरा": "zara",
}

# Particles that add politeness, not meaning ("haan ji", "ok sir"); dropped unless nothing else is left.
_PARTICLES = {"ji", "jee", "sir", "madam", "mam", "maam", "please", "plz", "pls", "bhai"}
_TOKEN = re.compile(r"[ऀ-ॿ]+|[a-z0-9]+")


def _collapse(word: str) -> str:
    """Stretched spellings fold together: nahhh/nahh -> nah, haaan -> han, okkk -> ok."""
    return re.sub(r"(.)\1+", r"\1", word)


def _build() -> dict[tuple[str, ...], str]:
    out: dict[tuple[str, ...], str] = {}
    for cat, phrases in _PHRASES.items():
        for p in phrases:
            out.setdefault(tuple(_collapse(w) for w in p.split()), cat)
    return out


_LEXICON = _build()
_LONGEST = max(len(k) for k in _LEXICON)


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("'", "").replace("’", "")
    out: list[str] = []
    for raw in _TOKEN.findall(t):
        w = _DEVA.get(raw, raw)
        out.extend(_collapse(x) for x in w.split())
    return out


def read(text: str) -> str | None:
    """The kind of short reply this is (YES, NO, SEND, LATER, HOLD, REPEAT, ACK, UNSURE), or None when it is anything else -
    a longer sentence, an unknown word, or two meanings that disagree."""
    toks = tokens(text)
    if not toks or len(toks) > MAX_WORDS:
        return None
    kept = [w for w in toks if w not in _PARTICLES] or toks      # "haan ji" -> "haan"; a lone "ji" stays
    cats: list[str] = []
    i = 0
    while i < len(kept):
        for n in range(min(_LONGEST, len(kept) - i), 0, -1):
            cat = _LEXICON.get(tuple(kept[i:i + n]))
            if cat:
                cats.append(cat)
                i += n
                break
        else:                       # an unknown word: not a short reply
            return None
    found = set(cats)
    if len(found) == 1:
        return cats[0]
    found.discard(ACK)              # "acha theek hai", "hmm nahi": the acknowledgement adds nothing
    if len(found) == 1:
        return next(iter(found))
    if found == {YES, SEND}:
        return SEND
    return None                     # "haan nahi", "ruko nahi": ambiguous, let the full reader (or a question) decide
