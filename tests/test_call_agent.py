"""Pure-logic checks for the live-call agent (no database, no network)."""
from datetime import date
from types import SimpleNamespace as NS

from app import contacts, knowledge, memory, memory_retrieval as mr, memory_write as mw, persona, retrieval, vectorstore, voice, voice_chat as vc

TARGET = NS(amount_inr=3000, ref="DEMO-F4BC68", due="10 Oct", script_kind="invoice_overdue")


# ── the reply checker ────────────────────────────────────────────────────────

def test_real_amount_is_allowed():
    assert vc.validate_reply("Aapka bakaya ₹3,000 hai, jo 10 Oct ko due tha.", TARGET, "hinglish") is None


def test_invented_amount_is_rejected():
    assert vc.validate_reply("Aapka bakaya ₹4,500 hai.", TARGET, "hinglish") == "unknown_number"


def test_numbers_the_customer_just_said_are_allowed():
    assert vc.validate_reply("Theek hai, 2 din ka time hai.", TARGET, "hinglish", "mujhe 2 din chahiye") is None


def test_money_words_need_an_approved_passage():
    text = "Ek baar ka late fee lagta hai."
    assert vc.validate_reply(text, TARGET, "hinglish") == "ungrounded_money_wording"
    assert vc.validate_reply(text, TARGET, "hinglish", grounding=["A one-time late fee of Rs 100 is added."]) is None


def test_grounded_numbers_are_allowed():
    assert vc.validate_reply("Late fee Rs 100 lagta hai.", TARGET, "hinglish", grounding=["late fee of Rs 100"]) is None
    assert vc.validate_reply("Late fee Rs 250 lagta hai.", TARGET, "hinglish", grounding=["late fee of Rs 100"]) == "unknown_number"


def test_an_english_call_rejects_hinglish_words_but_a_hinglish_call_allows_them():
    assert vc.validate_reply("As per our policy, we do not add a late fee.", TARGET, "en", grounding=["late fee"]) is None
    assert vc.validate_reply("Okay, hamare policy ke mutabiq, hum koi late fee nahi lagate.", TARGET, "en",
                             grounding=["late fee"]) == "wrong_language"
    assert vc.validate_reply("Hamari policy ke mutabiq koi late fee nahi lagta.", TARGET, "hinglish",
                             grounding=["late fee"]) is None


def test_a_memory_answer_may_not_add_names_it_was_never_given():
    known = "Customer said money arrives around the 5th. Spoke on 12 Oct."
    assert vc.invented_names("Aapne kaha tha ki aap Mars se hain.", known) == ["Mars"]
    assert vc.invented_names("Aapne 12 Oct ko salary 5 tareekh batayi thi.", known) == []
    assert vc.invented_names("Haan ji, Backstop Store ne kal call kiya tha.", known) == []
    assert vc.invented_names("Aapne Rahul ji ka naam liya tha.", known + " Rahul") == []


def test_threat_and_legal_wording_is_never_allowed():
    assert vc.validate_reply("Hum legal action lenge.", TARGET, "hinglish", grounding=["legal action"]) == "forbidden_wording"


def test_no_links_no_human_claims_right_script_and_length():
    assert vc.validate_reply("Dekhiye https://pay.me/x", TARGET, "hinglish") == "contains_link"
    assert vc.validate_reply("Main ek insaan hoon.", TARGET, "hinglish") == "claims_to_be_human"
    assert vc.validate_reply("आपका बकाया ₹3,000 है।", TARGET, "hinglish") == "wrong_script"
    assert vc.validate_reply("Your balance is 3000 rupees.", TARGET, "hi") == "wrong_script"
    assert vc.validate_reply("x" * 400, TARGET, "en") == "too_long"


# ── persona ──────────────────────────────────────────────────────────────────

def test_gender_forms_flip_both_ways():
    assert persona.apply_gender("main bhej deta hoon", "female") == "main bhej deti hoon"
    assert persona.apply_gender("main bhej deti hoon", "male") == "main bhej deta hoon"
    assert persona.apply_gender("मैं भेज रहा हूँ", "female") == "मैं भेज रही हूँ"


def test_filler_never_repeats_the_last_used():
    avoid = {"acha", "ji"}
    for i in range(12):
        out = vc.with_filler("Bakaya amount hai.", "hinglish", i, "kitna hai", avoid=avoid)
        assert persona.lead(out) not in avoid


def test_at_most_one_touch_and_repeat_is_untouched():
    assert vc.with_filler("Acha, theek hai.", "hinglish", 3) == "Acha, theek hai."
    assert vc.with_filler("script text", "hinglish", 3, repeat=True) == "script text"
    kind = vc.with_filler("Bakaya amount hai.", "hinglish", 1, "meri job chali gayi")
    assert kind.startswith(tuple(persona.EMPATHY["hinglish"]))


def test_reask_varies():
    first = persona.reask("invoice_overdue", "hinglish", 0)
    assert persona.reask("invoice_overdue", "hinglish", 0, first) != first


# ── understanding words ──────────────────────────────────────────────────────

def test_commitment_vs_date_only():
    assert vc.has_commitment("ok 10 tareekh tak kar dunga")
    assert not vc.has_commitment("meri salary 5 tareekh ko aati hai")
    assert vc.is_yes("haan theek hai note kar lo")
    assert not vc.is_yes("nahi 7 ko")
    assert vc.is_yes("हा हा नोट कर लो")                       # what speech-to-text returns for "haan haan note kar lo"
    assert vc.is_yes("हाँ ठीक है") and vc.is_yes("yes yes") and vc.is_yes("ओके")
    assert not vc.is_yes("मैंने कहा था 7 तारीख") and not vc.is_yes("नहीं 7 को")


def test_keyword_classifier_edge_cases():
    today = date(2026, 10, 18)
    k = lambda t: voice.keyword_classify(t, today)
    assert k("Please send me the payment link now. I will pay.")["intent"] == "PAY_NOW"
    assert k("agar main pay nahi karunga to aap legal action loge?")["intent"] == "REFUSED"
    assert k("I will pay by day after tomorrow") == {"intent": "PROMISE", "promise_date": date(2026, 10, 20)}
    assert k("अभी लिंक भेज दो, मैं पे कर देता हूँ")["intent"] == "PAY_NOW"
    assert k("yeh debit maine authorise nahi kiya")["intent"] == "DISPUTE"
    assert k("this was an unauthorised debit")["intent"] == "DISPUTE"


def test_question_keywords_and_honesty_about_being_an_ai():
    assert vc.keyword_question("kya aap robot ho") == "Q_WHO"
    assert vc.keyword_question("mujhe kisi insaan se baat karni hai") == "Q_OTHER"
    assert vc.keyword_question("refund kaise milega") == "Q_POLICY"


# ── knowledge + memory + search helpers ──────────────────────────────────────

def test_chunker_keeps_headings_and_pulls_out_aliases():
    doc = "# Policy\n\n## Refunds\nRefunds take 5 days.\nAlso asked as: paisa wapas; refund kaise\n\n## Hours\nOpen 9 to 6."
    chunks = knowledge.chunk_text(doc)
    assert [c[0] for c in chunks] == ["Refunds", "Hours"]
    assert chunks[0][2] == "paisa wapas; refund kaise"
    assert "Also asked" not in chunks[0][1]            # aliases are searched, never spoken


def test_translation_lines_travel_with_the_passage_and_are_never_part_of_it():
    doc = ("## Time\nYou can ask for 7 days.\nAlso asked as: kitna time\nHinglish: Aap 7 din ka time maang sakte hain.\n"
           "Hindi: आप 7 दिन का समय माँग सकते हैं।\n\n## Hours\nOpen 9 to 6.")
    chunks = knowledge.chunk_text(doc)
    assert chunks[0].translations == {"hinglish": "Aap 7 din ka time maang sakte hain.", "hi": "आप 7 दिन का समय माँग सकते हैं।"}
    assert "Hinglish" not in chunks[0][1] and chunks[1].translations == {}
    heading, text, aliases = chunks[0]                  # still a plain 3-tuple wherever it is unpacked
    assert heading == "Time" and aliases == "kitna time"


def test_fallback_reads_the_merchants_wording_in_the_call_language():
    hit = NS(extra={"translations": {"hinglish": "Aap 7 din tak ka time le sakte hain.", "hi": "आप 7 दिन तक का समय ले सकते हैं।"}})
    assert vc._localized(hit, "hinglish").startswith("Aap 7 din")
    assert vc._localized(hit, "hi").startswith("आप 7")
    assert vc._localized(hit, "en") is None and vc._localized(NS(extra={}), "hinglish") is None
    assert vc._cut("A. " + "B" * 400, 50) == "A."


def test_comments_for_the_merchant_are_never_a_passage():
    doc = "# Title\n<!-- Review these numbers before going live. -->\n\n## A\nReal text.\n<!--\nmulti line\nnote\n-->\n## B\nMore."
    chunks = knowledge.chunk_text(doc)
    assert [c[0] for c in chunks] == ["A", "B"] and all("Review" not in c[1] and "note" not in c[1] for c in chunks)


def test_chunker_splits_long_sections():
    long = "## Big\n" + " ".join(f"Sentence number {i} is here." for i in range(60))
    assert len(knowledge.chunk_text(long, target=300)) > 3


def test_memory_scrub_removes_contact_details_and_sensitive_topics():
    assert memory.scrub("Call 9876543210 after 5") == "Call [removed] after 5"
    assert memory.scrub("Mother is in hospital") is None
    assert memory.scrub("Prefers evening calls.") == "Prefers evening calls."
    assert len(memory.scrub("x" * 400)) == memory.MAX_NOTE_CHARS


def test_rule_notes_never_keep_hardship_details():
    notes = memory._rule_notes(["meri job chali gayi hai", "salary 5 tareekh ko aati hai"], None)
    kinds = {n["kind"] for n in notes}
    assert {"hardship_flag", "stated_constraint"} <= kinds
    assert all("job" not in n["text"].lower() for n in notes)


def test_search_helpers():
    assert vectorstore._tsquery("late fee kya hai?") == "late | fee"
    assert vectorstore._tsquery("kya hai") is None
    assert retrieval._overlap("refund kaise milega", "Refund policy: refunds are sent back") > 0.3


# ── tiered memory: write side ────────────────────────────────────────────────

def _turns(n):
    return [(i, "agent" if i % 2 == 0 else "customer", f"line {i}, with \"quotes\" and 9876543210") for i in range(n)]


def test_windows_are_the_turns_word_for_word_and_cover_everything():
    turns = _turns(9)
    wins = mw.build_windows(turns, size=4)
    assert wins[0]["text"].splitlines()[0] == 'Agent: line 0, with "quotes" and 9876543210'   # not scrubbed
    assert wins[0]["turn_from"] == 0 and wins[-1]["turn_to"] == 8
    covered = {n for w in wins for n in range(w["turn_from"], w["turn_to"] + 1)}
    assert covered == set(range(9))
    assert all(a["turn_to"] == b["turn_from"] for a, b in zip(wins, wins[1:]))           # one-turn overlap
    assert mw.build_windows([], size=4) == []
    assert len(mw.build_windows(_turns(3), size=4)) == 1


def test_fact_conflict_resolution():
    assert mw.fact_action(None, "day 5") == "create"
    assert mw.fact_action("Day 5 ", "day  5") == "confirm"
    assert mw.fact_action("day 5", "day 10") == "supersede"


def test_model_output_is_allow_listed_scrubbed_and_clamped():
    facts = mw.clean_facts([
        {"key": "salary_day", "value": "around day 5, call 9876543210", "confidence": 7},
        {"key": "salary_day", "value": "day 6"},                          # second value for a key is dropped
        {"key": "favourite_colour", "value": "blue"},                     # key not allowed
        {"key": "preference", "value": "mother in hospital"},             # sensitive subject
        "not a dict"])
    assert facts == [{"key": "salary_day", "value": "around day 5, call [removed]", "confidence": 1.0}]
    ep = mw.clean_episode({"summary": "Promised to pay.", "importance": "high", "key_events": ["a", None, "b"],
                           "facts": [{"key": "hardship_flag", "value": "be gentle"}]})
    assert ep["importance"] == 0.5 and ep["key_events"] == ["a", "b"] and ep["facts"][0]["key"] == "hardship_flag"


def test_rule_fallback_summary_and_facts_stay_clean():
    lines = ["meri salary 5 tareekh ko aati hai", "mother is in hospital", "meri job chali gayi hai"]
    summary = mw.extractive_summary(lines, "promise_created:2026-10-08")
    assert "2026-10-08" in summary and "hospital" not in summary and "job" not in summary
    keys = {f["key"] for f in mw.rule_facts(lines, None)}
    assert "salary_day" in keys
    assert mw.rule_importance("invoice_disputed", False) > mw.rule_importance(None, False)


def test_only_the_current_consent_wording_unlocks_tiered_memory():
    v1 = NS(memory_consent=True, memory_consent_version=None)
    v2 = NS(memory_consent=True, memory_consent_version=contacts.MEMORY_CONSENT_TEXT_VERSION)
    off = NS(memory_consent=False, memory_consent_version=contacts.MEMORY_CONSENT_TEXT_VERSION)
    assert contacts.has_full_memory_consent(v2)
    assert not contacts.has_full_memory_consent(v1)
    assert not contacts.has_full_memory_consent(off)
    assert not contacts.has_full_memory_consent(None)


# ── tiered memory: read side ─────────────────────────────────────────────────

def test_recall_cues_catch_questions_about_the_past_only():
    for yes in ("pichli baar kya baat hui thi", "what did we discuss last time", "maine kaha tha 5 tareekh", "पिछली बार आपने क्या कहा"):
        assert mr.has_recall_cue(yes), yes
    for no in ("kitna bakaya hai", "link bhej do", "aapne kya kaha"):
        assert not mr.has_recall_cue(no), no
    assert vc.keyword_question("pichli baar kya baat hui thi") == "Q_MEMORY"
    assert vc.keyword_question("pichli baar maine kya kaha tha?") == "Q_MEMORY"        # not "say that again"
    assert vc.keyword_question("maine kaunsi tareekh bataayi thi?") == "Q_MEMORY"       # not "who are you"
    assert vc.keyword_question("aap kaun bol rahe ho") == "Q_WHO"
    assert vc.keyword_question("kya kaha aapne") == "Q_REPEAT"


def test_score_prefers_recent_important_confident_relevant():
    base = mr.score_item(0.8, 10, 0.5, 1.0)
    assert mr.score_item(0.8, 120, 0.5, 1.0) < base           # older
    assert mr.score_item(0.8, 10, 0.9, 1.0) > base            # more important
    assert mr.score_item(0.8, 10, 0.5, 0.5) < base            # less sure
    assert mr.score_item(0.4, 10, 0.5, 1.0) < base            # less relevant
    assert abs(mr.recency_decay(60, 60) - 0.5) < 1e-9
    assert mr.relevance_of(0.7, 0.001) == 0.7 and 0 < mr.relevance_of(None, 1 / 61) <= 1


def test_budget_drops_a_line_rather_than_cutting_it():
    out = mr.fit_budget(["a" * 50, "b" * 300, "c" * 40], budget=120)
    assert out == ["a" * 50, "c" * 40]


def test_memory_block_is_tagged_so_a_reply_can_cite_it():
    m = mr.Memory(core=["best time to call: evening"],
                  recalled=[{"mid": "m1", "kind": "window", "when": "12 Oct", "text": "Customer: kal tak kar dunga"}])
    lines = m.prompt_lines()
    assert lines[0].startswith("[c1] best time") and lines[1].startswith("[m1] (word for word, your call on 12 Oct)")
    assert m.recalled_ids == {"m1"} and m.citable_ids == {"c1", "m1"} and "kal tak" in m.known_text() and "12 Oct" in m.known_text() and not m.empty()
    assert mr.Memory().empty()


# ── knowledge store upgrades ─────────────────────────────────────────────────

def test_identical_text_hashes_the_same_whatever_the_spacing():
    assert knowledge.content_hash("Refunds take  5 days.\n") == knowledge.content_hash("Refunds take 5 days.")
    assert knowledge.content_hash("Refunds take 5 days.") != knowledge.content_hash("Refunds take 7 days.")


def test_split_sections_remember_the_whole_section():
    long = "## Big\n" + " ".join(f"Sentence number {i} is here." for i in range(60)) + "\n\n## Small\nOpen 9 to 6."
    chunks = knowledge.chunk_text(long, target=300)
    sections = knowledge.section_texts(chunks)
    big = [i for i, c in enumerate(chunks) if c[0] == "Big"]
    assert len(big) > 3 and all(sections[i] and "Sentence number 59" in sections[i] for i in big)
    small = [i for i, c in enumerate(chunks) if c[0] == "Small"]
    assert all(sections[i] is None for i in small)         # a one-piece section already is the section
