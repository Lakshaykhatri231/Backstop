"""Fixed spoken lines: complete in every language, in the right language, honest about being an AI, nothing forbidden."""
import re

from evals.harness import Recorder

SAMPLE = dict(name="Rahul", merchant="Backstop Store", amount="₹3,000", due="10 Oct", ref="INV-1042", date="12 Oct", when="12 Oct")


def _deva_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 0.0


def _flat(d, path=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{path}{k}.")
        else:
            yield f"{path}{k}", v


def run(ctx):
    from app import persona, voice, voice_chat as vc
    R = Recorder("fixed_lines", "Every pre-written line exists in all three languages, formats cleanly, stays in its language, and never says anything forbidden.")
    forbidden = [w.lower() for w in vc._ALWAYS_FORBIDDEN] + [w.lower() for w in vc._MONEY_WORDS]
    n = ok_lang = ok_clean = 0

    # 1. the same keys in every language
    base = set(k for k, _ in _flat(vc._ANSWERS["hinglish"]))
    for lang in ("hi", "en"):
        missing = base - set(k for k, _ in _flat(vc._ANSWERS[lang]))
        R.case(f"answers_complete:{lang}", not missing, f"missing keys: {sorted(missing)}", critical=True)

    # 2. each line: formats, right script/language, no forbidden wording
    lines = [(f"answer:{lang}:{k}", lang, v) for lang, d in vc._ANSWERS.items() for k, v in _flat(d)]
    lines += [(f"opening:{kind}:{lang}", lang, tpl) for (kind, lang), tpl in voice._TEMPLATES.items()]
    for id_, lang, text in lines:
        n += 1
        try:
            shown = text.format(**SAMPLE)
        except (KeyError, IndexError) as e:
            R.case(id_, False, f"does not format: missing {e}", critical=True)
            continue
        problems = []
        if "{" in shown or "}" in shown:
            problems.append("template braces left")
        ratio = _deva_ratio(shown)
        if lang == "hi" and ratio < 0.35:
            problems.append(f"Hindi line is only {ratio:.0%} Devanagari")
        if lang in ("hinglish", "en") and ratio > 0:       # Roman-only: a single stray Devanagari word breaks the voice and the language rule
            problems.append(f"{lang} line contains Devanagari ({ratio:.0%})")
        if lang == "en" and vc._HINGLISH_WORDS.search(shown):
            problems.append(f"English line has Hinglish words: {vc._HINGLISH_WORDS.search(shown).group(0)!r}")
        low = shown.lower()
        hit = [w for w in forbidden if w in low]
        if hit:
            problems.append(f"forbidden wording {hit[:2]}")
        ok_lang += not any("Devanagari" in p or "Hinglish words" in p for p in problems)
        ok_clean += not hit
        R.case(id_, not problems, "; ".join(problems), critical=True)
    R.rate("language_correct_rate", ok_lang, n)
    R.rate("forbidden_free_rate", ok_clean, n)
    R.require("language_correct_rate", "==", 1.0)
    R.require("forbidden_free_rate", "==", 1.0)

    # 3. every opening says it is an AI and that the call may be recorded
    for (kind, lang), tpl in voice._TEMPLATES.items():
        s = voice.build_script(kind, lang, "Rahul", 3000, due="10 Oct", ref="INV-1042")
        has_ai = bool(re.search(r"\bAI\b", s))
        has_rec = bool(re.search(r"record|रिकॉर्ड", s, re.I))
        R.case(f"opening_discloses:{kind}:{lang}", has_ai and has_rec, f"AI disclosure={has_ai}, recording notice={has_rec}", critical=True)
        anon = voice.build_script(kind, lang, "Customer", 3000, due="10 Oct", ref="INV-1042")
        R.case(f"opening_anonymous:{kind}:{lang}", "Customer" not in anon and "  " not in anon, f"greeting for an unknown name reads: {anon[:50]!r}")

    # 4. persona pools and gender
    for pool_name in ("ACK", "EMPATHY", "THINKING"):
        pool = getattr(persona, pool_name)
        for lang in ("hinglish", "hi", "en"):
            R.case(f"pool:{pool_name}:{lang}", bool(pool.get(lang)) and not any(w in " ".join(pool[lang]).lower() for w in forbidden),
                   "empty or contains forbidden wording")
    sample = "main bhej deta hoon aur samajh gaya"
    fem = persona.apply_gender(sample, "female")
    R.case("gender_female", "deti hoon" in fem and "gayi" in fem, fem)
    R.case("gender_roundtrip", persona.apply_gender(fem, "male") == sample, persona.apply_gender(fem, "male"))
    R.case("gender_idempotent", persona.apply_gender(fem, "female") == fem)
    return R.result
