"""Memory read path: the profile card, recall cues, ranking, the size budget, consent, expiry, links."""
from datetime import datetime, timedelta

from evals import fixtures as fx
from evals.harness import Recorder

CUES_YES = ["pichli baar maine kya kaha tha", "pichli baar kya baat hui thi", "last time what did I say", "what did we talk about last time",
            "maine kaunsi tareekh bataayi thi", "पिछली बार मैंने क्या कहा था", "aapko yaad hai maine kya bola", "you said something earlier",
            "maine kaha tha 5 tareekh", "previous call mein kya hua tha"]
CUES_NO = ["kitna bakaya hai", "link bhej do", "main kal tak de dunga", "late fee lagega kya", "aap kaun bol rahe ho", "mujhe time chahiye",
           "yeh kis liye hai", "payment kaise karun", "haan theek hai", "I will pay by Friday"]


def run(ctx):
    from app import memory_retrieval as mr
    from app.config import settings
    from app.database import SessionLocal
    from app.rag_models import ConversationEpisode, EpisodeChunk
    fx.patch_world()
    R = Recorder("memory_read", "The agent is handed the right memory at the right time: a compact profile always, past calls only when asked, within budget, never without consent.")
    db = SessionLocal()
    try:
        # cue detection
        yes = sum(mr.has_recall_cue(t) for t in CUES_YES)
        no = sum(not mr.has_recall_cue(t) for t in CUES_NO)
        for t in CUES_YES:
            R.case(f"cue:{t[:40]}", mr.has_recall_cue(t), "a question about the past was not recognised", critical=True)
        for t in CUES_NO:
            R.case(f"no_cue:{t[:40]}", not mr.has_recall_cue(t), "an ordinary line triggered a memory search")
        R.rate("cue_recall", yes, len(CUES_YES))
        R.rate("cue_precision_on_negatives", no, len(CUES_NO))
        R.require("cue_recall", "==", 1.0)
        R.require("cue_precision_on_negatives", "==", 1.0)

        # the scenario: a returning customer with two earlier calls
        cp = fx.contact(db)
        sal = {"summary": "Customer said money arrives around the 5th and asked for evening calls. They promised to pay by the 8th.", "tone_arc": "tense -> relieved",
               "importance": 0.8, "key_events": ["Money arrives around the 5th", "Promised the 8th"],
               "facts": [{"key": "salary_day", "value": "Money usually arrives around day 5 of the month."},
                         {"key": "preferred_call_time", "value": "Prefers to be called in the evening."}]}
        link = {"summary": "Customer asked for the payment link and said they would pay in the evening.", "importance": 0.4, "facts": []}
        subj = "INV-READ-1"
        c1, e1 = fx.episode(db, cp, [("agent", "Namaste, invoice pending hai."), ("customer", "meri salary 5 tareekh ko aati hai, shaam ko call karna"),
                                     ("agent", "Theek hai, kab tak?"), ("customer", "8 tareekh tak kar dunga")], summary=sal, routed="promise_created:2026-10-08", days_ago=20, subject_id=subj)
        c2, e2 = fx.episode(db, cp, [("agent", "Ek aur payment pending hai."), ("customer", "haan link bhej do, shaam ko pay kar dunga")], summary=link,
                            routed="payment_link_sent", days_ago=9, subject_id=subj)

        # profile card
        card, trace = mr.core_card(db, cp.id)
        joined = " ".join(card).lower()
        R.case("card:has_salary_fact", "day 5" in joined, f"card: {card}", critical=True)
        R.case("card:has_call_time_fact", "evening" in joined, f"card: {card}")
        R.case("card:has_last_call_line", any(c.startswith("last call") for c in card), f"card: {card}")
        R.case("card:within_size", len(card) <= mr.MAX_CORE_FACTS + 1, f"{len(card)} lines")
        R.case("card:trace_holds_ids_not_text", all(isinstance(v, (list, str, type(None))) for v in trace.values()) and "day 5" not in str(trace), f"trace: {trace}")

        # no cue -> profile only; cue -> recall
        quiet = mr.build(db, cp, "kitna bakaya hai")
        R.case("build:no_cue_no_recall", not quiet.recalled and bool(quiet.core), f"recalled {len(quiet.recalled)}", critical=True)
        loud = mr.build(db, cp, "pichli baar maine salary ke baare mein kya kaha tha")
        R.case("build:cue_recalls", bool(loud.recalled), "nothing was recalled on a recall question", critical=True)
        windows = [r for r in loud.recalled if r["kind"] == "window"]
        R.case("recall:right_call_ranked_first", bool(windows) and "salary" in windows[0]["text"].lower(), f"top window: {windows[0]['text'][:80] if windows else None}")
        R.case("recall:last_summary_included", any(r["kind"] == "summary" for r in loud.recalled), "")
        R.case("recall:items_carry_ids_and_dates", all(r.get("mid") and r.get("when") and r.get("id") for r in loud.recalled), "")
        # with room for only ONE matched window, the call linked to it (the one before) is offered as a summary
        hop = mr.recall(db, cp.id, "payment link kab bheja tha", k=1)
        R.case("recall:one_hop_link_expansion", any(r["kind"] == "summary" and r["episode_id"] == e1.id for r in hop),
               f"the call linked to the matched one was not offered: {[(r['kind'], r['episode_id'][:6]) for r in hop]}")

        # budget
        many = fx.contact(db)
        long_line = "yeh ek lambi baat hai jisme bahut saare shabd hain " * 6
        for d in range(12):
            fx.episode(db, many, [("agent", "Namaste"), ("customer", f"{long_line} salary {d}")], summary={"summary": "Long call. " * 30, "importance": 0.5, "facts": []}, days_ago=d)
        big = mr.build(db, many, "pichli baar salary ke baare mein kya baat hui thi")
        total = sum(len(l) + 1 for l in big.prompt_lines())
        R.case("budget:prompt_within_budget", total <= settings.memory_budget_chars, f"{total} chars (budget {settings.memory_budget_chars})", critical=True)
        R.case("budget:fit_drops_whole_lines", mr.fit_budget(["a" * 50, "b" * 300, "c" * 40], 120) == ["a" * 50, "c" * 40], "")

        # consent
        # memory that EXISTS must still not be read once the customer is no longer on the current wording (stored first, consent changed after)
        for label in ("old_wording", "withdrawn"):
            other = fx.contact(db)
            fx.episode(db, other, [("agent", "Namaste"), ("customer", "meri salary 5 tareekh ko aati hai")], summary=sal)
            R.case(f"consent:{label}:there_is_something_stored", fx.counts(db, other.id)["episodes"] == 1 and not mr.build(db, other, "pichli baar kya kaha tha").empty(), "setup: nothing stored to protect")
            if label == "old_wording":
                other.memory_consent_version = None             # agreed only to the older wording
            else:
                other.memory_consent = False                    # withdrew (without the delete having run yet)
            db.commit()
            R.case(f"consent:{label}_reads_nothing", mr.build(db, other, "pichli baar kya kaha tha").empty(), "memory was returned without the current consent", critical=True)
        R.case("consent:no_contact_reads_nothing", mr.build(db, None, "pichli baar").empty(), "", critical=True)

        # expiry
        old_ep = db.query(ConversationEpisode).filter_by(id=e1.id).first()
        old_ep.expires_at = datetime.utcnow() - timedelta(days=1)
        db.query(EpisodeChunk).filter_by(episode_id=e1.id).update({"expires_at": datetime.utcnow() - timedelta(days=1)})
        db.commit()
        after = mr.recall(db, cp.id, "salary 5 tareekh")
        R.case("expiry:expired_call_not_recalled", all(r["episode_id"] != e1.id for r in after), "an expired call was recalled", critical=True)

        # scoring
        R.case("score:newer_beats_older", mr.score_item(0.8, 5, 0.5) > mr.score_item(0.8, 200, 0.5), "")
        R.case("score:important_beats_minor", mr.score_item(0.8, 5, 0.9) > mr.score_item(0.8, 5, 0.1), "")
        R.case("score:relevant_beats_vague", mr.score_item(0.9, 5, 0.5) > mr.score_item(0.3, 5, 0.5), "")
        R.case("score:half_life", abs(mr.recency_decay(settings.memory_half_life_days) - 0.5) < 1e-9, "")
        return R.result
    finally:
        db.close()
