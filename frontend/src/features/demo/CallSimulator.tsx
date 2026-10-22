import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import { takeOpening } from "./callAudio";
import { looksUnfinished, useLiveCall } from "./useLiveCall";
import {
  fetchCallAudio, fillerUrl, getSpeechConfig, replyToCall, sendTurn, transcribeAudio,
  type CallResult, type RingingCall, type TurnTimings,
} from "@/lib/api/demo";

// Browser speech is only a convenience: every action also works by typing.
// Nothing here talks to a telephony provider. The agent's lines are always
// text the server wrote (the script or a fixed answer); this page only plays
// them and sends back what the "customer" says.
type RecognitionLike = {
  lang: string;
  interimResults: boolean;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

function getRecognition(): (new () => RecognitionLike) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => RecognitionLike;
    webkitSpeechRecognition?: new () => RecognitionLike;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const speechLang = (language: string) => (language === "hi" ? "hi-IN" : "en-IN");

const QUICK_REPLIES: { label: string; text: string; group: "ask" | "decide" }[] = [
  { label: "How much is it?", text: "Kitna amount hai?", group: "ask" },
  { label: "Why is this?", text: "Yeh kis liye hai?", group: "ask" },
  { label: "Who are you?", text: "Aap kaun bol rahe ho?", group: "ask" },
  { label: "Can I pay half?", text: "Kya main aadha pay kar sakta hoon?", group: "ask" },
  { label: "Say that again", text: "Dobara bolo, samajh nahi aaya.", group: "ask" },
  { label: "Your manager", text: "Mujhe aapke manager se baat karni hai.", group: "ask" },
  { label: "Will pay in 2 days", text: "Haan ji, parso tak payment kar dunga, pakka.", group: "decide" },
  { label: "Send me the link", text: "Haan ji, link bhej do, abhi payment karta hoon.", group: "decide" },
  { label: "Dispute", text: "Yeh bill galat hai, maal damaged mila tha, hum pay nahi karenge.", group: "decide" },
  { label: "Wrong number", text: "Aap galat number pe call kar rahe hain.", group: "decide" },
  { label: "Busy, call later", text: "Abhi busy hoon, thodi der baad call kariye.", group: "decide" },
  { label: "Refuse", text: "Nahi, hum pay nahi karenge, dobara call mat karna.", group: "decide" },
];

type Line = {
  who: "agent" | "you"; text: string; turn: number; kind?: string | null; timings?: TurnTimings | null | undefined;
  sources?: { title: string; heading?: string | null; score: number }[] | null | undefined;
};

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : "Something went wrong";
}

export function CallSimulator({ call, onDone }: { call: RingingCall; onDone: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [lines, setLines] = useState<Line[]>([{ who: "agent", text: call.script, turn: 0 }]);
  const [result, setResult] = useState<CallResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [listening, setListening] = useState(false);
  const [autoSend, setAutoSend] = useState(true);
  const [autoPlay, setAutoPlay] = useState(true);
  const recRef = useRef<RecognitionLike | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const mediaRef = useRef<MediaRecorder | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const [voiceNote, setVoiceNote] = useState("");
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [handsFree, setHandsFree] = useState(true);
  const [stage, setStage] = useState<"idle" | "transcribing" | "thinking">("idle");
  const playingRef = useRef(false);            // an agent clip is audible right now
  const busyRef = useRef(false);               // a turn is in flight
  const doneRef = useRef(false);
  const queuedRef = useRef<{ text: string; stt_ms: number; interrupted: boolean } | null>(null);
  const pendingRef = useRef<{ text: string; stt_ms: number; interrupted: boolean } | null>(null);
  const holdRef = useRef<number | null>(null);
  const fillerRef = useRef<HTMLAudioElement | null>(null);
  const fillerTimer = useRef<number | null>(null);
  const cfgQ = useQuery({ queryKey: ["demo", "speech-config"], queryFn: getSpeechConfig, staleTime: 60_000 });
  const cloudVoice = cfgQ.data?.tts === "azure";
  const cloudEar = cfgQ.data?.stt === "groq" && typeof MediaRecorder !== "undefined";
  const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;
  const Recog = typeof window !== "undefined" ? getRecognition() : null;
  const customerTurns = lines.filter((l) => l.who === "you").length;

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines.length]);

  // The agent speaks first, as soon as the call is picked up (the Accept click
  // is the user gesture that lets audio start).
  useEffect(() => {
    void speak(call.script, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Stop any speech / mic when the call goes away.
  useEffect(() => {
    return () => {
      if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
      recRef.current?.stop();
      audioRef.current?.pause();
      fillerRef.current?.pause();
      if (fillerTimer.current !== null) window.clearTimeout(fillerTimer.current);
      if (holdRef.current !== null) window.clearTimeout(holdRef.current);
      if (mediaRef.current && mediaRef.current.state !== "inactive") mediaRef.current.stop();
    };
  }, []);

  function browserSpeak(t: string) {
    if (!canSpeak) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(t);
    u.lang = speechLang(call.language);
    window.speechSynthesis.speak(u);
  }

  // One voice at a time. Every new line cancels whatever is still playing (the
  // browser voice AND any cloud clip), and a stale request that finishes late
  // is dropped, so two voices can never overlap.
  const speakSeq = useRef(0);
  const clips = useRef(new Map<number, Blob>());   // replays reuse the clip (saves the free voice allowance)

  function stopAllAudio() {
    if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
    audioRef.current?.pause();
    playingRef.current = false;
  }

  function stopFiller() {
    if (fillerTimer.current !== null) window.clearTimeout(fillerTimer.current);
    fillerTimer.current = null;
  }

  // If the reply is slow, a short "one moment" clip keeps the line from going
  // dead (cloud voice only, so it is the same voice as the reply).
  function armFiller() {
    stopFiller();
    if (!cloudVoice) return;
    fillerTimer.current = window.setTimeout(() => {
      const a = new Audio(fillerUrl(call.language, Math.floor(Math.random() * 3)));
      fillerRef.current = a;
      a.onended = () => { fillerRef.current = null; };
      void a.play().catch(() => { fillerRef.current = null; });
    }, 700);
  }

  async function letFillerFinish() {
    const f = fillerRef.current;
    if (!f || f.ended) return;
    await new Promise<void>((resolve) => {
      const t = window.setTimeout(resolve, 900);
      f.addEventListener("ended", () => { window.clearTimeout(t); resolve(); }, { once: true });
    });
  }

  async function speak(t: string, turn: number) {
    const seq = ++speakSeq.current;
    setVoiceNote("");
    stopAllAudio();
    // Never guess: ask the server which voice is configured, even if the page's
    // copy of the setting has not loaded yet.
    let cloud = cfgQ.data?.tts === "azure";
    if (cfgQ.data === undefined) {
      try {
        cloud = (await qc.fetchQuery({ queryKey: ["demo", "speech-config"], queryFn: getSpeechConfig, staleTime: 60_000 })).tts === "azure";
      } catch {
        cloud = false;
      }
    }
    if (seq !== speakSeq.current) return;
    if (cloud) {
      try {
        const blob =
          clips.current.get(turn) ??
          (turn === 0 ? await takeOpening(call.call_id) : null) ??
          (await fetchCallAudio(call.call_id, turn || undefined));
        clips.current.set(turn, blob);
        if (seq !== speakSeq.current) return;
        await letFillerFinish();
        if (seq !== speakSeq.current) return;
        stopAllAudio();
        const a = new Audio(URL.createObjectURL(blob));
        audioRef.current = a;
        a.onplay = () => { playingRef.current = true; };
        a.onended = () => { playingRef.current = false; };
        a.onpause = () => { playingRef.current = false; };
        await a.play();
        return;
      } catch (e) {
        if (seq !== speakSeq.current) return;
        setVoiceNote(`Cloud voice unavailable (${e instanceof Error ? e.message : "error"}); using the browser voice.`);
      }
    }
    browserSpeak(t);
  }

  async function submit(said: string, meta: { stt_ms?: number; interrupted?: boolean } = {}) {
    const t = said.trim();
    if (doneRef.current) return;
    if (busyRef.current) {   // a reply is still being prepared: send this right after it
      if (t) queuedRef.current = { text: t, stt_ms: meta.stt_ms ?? 0, interrupted: !!meta.interrupted };
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setStage("thinking");
    setError("");
    setText("");
    if (t) setLines((l) => [...l, { who: "you", text: t, turn: -1 }]);
    armFiller();
    try {
      const r = await sendTurn(call.call_id, t, { ...(meta.stt_ms != null ? { stt_ms: meta.stt_ms } : {}), interrupted: !!meta.interrupted });
      stopFiller();
      if (r.agent) {
        const a = r.agent;
        setLines((l) => [...l, { who: "agent", text: a.text, turn: a.turn_no, kind: a.kind, timings: a.timings, sources: a.sources }]);
        if (autoPlay) void speak(a.text, a.turn_no);
      }
      if (r.done) {
        doneRef.current = true;
        setResult(r);
        await qc.invalidateQueries();
      }
    } catch (e) {
      stopFiller();
      setError(errText(e));
    } finally {
      busyRef.current = false;
      setBusy(false);
      setStage("idle");
      const q = queuedRef.current;
      queuedRef.current = null;
      if (q && !doneRef.current) void submit(q.text, { stt_ms: q.stt_ms, interrupted: q.interrupted });
    }
  }

  // ── hands-free: what to do with each stretch of speech the detector finds ──
  function clearHold() {
    if (holdRef.current !== null) window.clearTimeout(holdRef.current);
    holdRef.current = null;
  }

  function flushPending() {
    clearHold();
    const p = pendingRef.current;
    pendingRef.current = null;
    if (p) void submit(p.text, { stt_ms: p.stt_ms, interrupted: p.interrupted });
  }

  function holdPending() {
    clearHold();
    holdRef.current = window.setTimeout(flushPending, 700);
  }

  async function handleUtterance(wav: Blob, info: { interrupted: boolean }) {
    if (doneRef.current) return;
    if (!cloudVoice && typeof window !== "undefined" && "speechSynthesis" in window && window.speechSynthesis.speaking) {
      return;   // the browser voice cannot be told apart from the customer (echo); wait for it to finish
    }
    clearHold();
    setStage("transcribing");
    const t0 = performance.now();
    try {
      const heard = await transcribeAudio(wav, call.language, call.call_id);
      const stt_ms = Math.round(performance.now() - t0);
      setStage(busyRef.current ? "thinking" : "idle");
      if (!heard.text) {
        if (pendingRef.current) holdPending();
        return;   // noise, silence or a blip: keep listening
      }
      const prev = pendingRef.current;
      pendingRef.current = null;
      const merged = prev ? `${prev.text} ${heard.text}` : heard.text;
      const interrupted = info.interrupted || !!prev?.interrupted;
      if (looksUnfinished(merged)) {
        pendingRef.current = { text: merged, stt_ms, interrupted };
        holdPending();      // wait a moment for the rest of the sentence
        return;
      }
      void submit(merged, { stt_ms, interrupted });
    } catch (e) {
      setStage("idle");
      setVoiceNote(e instanceof Error ? e.message : "Could not hear that - try again or type it.");
    }
  }

  const live = useLiveCall(
    handsFree && cloudEar && !result,
    {
      isAgentSpeaking: () =>
        playingRef.current || (typeof window !== "undefined" && "speechSynthesis" in window && window.speechSynthesis.speaking),
      onSpeechStart: () => clearHold(),
      onBargeIn: () => {
        const a = audioRef.current;
        if (a) { a.volume = 0; a.pause(); }
        if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
        playingRef.current = false;
      },
      onUtterance: (wav, info) => void handleUtterance(wav, info),
      onMisfire: () => { if (pendingRef.current) holdPending(); },
    },
  );
  const handsFreeOn = handsFree && cloudEar && live.status !== "error" && live.status !== "off";

  async function noAnswer() {
    setBusy(true);
    try {
      const r = await replyToCall(call.call_id, "");
      doneRef.current = true;
      setResult(r);
      await qc.invalidateQueries();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggleRecord() {
    if (recording) {
      mediaRef.current?.stop();
      return;
    }
    setVoiceNote("");
    audioRef.current?.pause();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        setTranscribing(true);
        try {
          const heard = await transcribeAudio(new Blob(chunks, { type: rec.mimeType || "audio/webm" }), call.language, call.call_id);
          if (!heard.text) setVoiceNote("Nothing was heard. Try again or type the reply.");
          else if (autoSend) void submit(heard.text);
          else setText(heard.text);
        } catch (e) {
          setVoiceNote(e instanceof Error ? e.message : "Could not transcribe that.");
        } finally {
          setTranscribing(false);
        }
      };
      mediaRef.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      setVoiceNote("Microphone access was blocked. Allow it in the browser, or type the reply.");
    }
  }

  function listen() {
    if (!Recog) return;
    const rec = new Recog();
    rec.lang = speechLang(call.language);
    rec.interimResults = false;
    rec.onresult = (e) => {
      const heard = e.results[0]?.[0]?.transcript ?? "";
      if (heard) setText(heard);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    recRef.current = rec;
    setListening(true);
    rec.start();
  }

  return (
    <div className="fixed inset-0 z-50 bg-ink/50 flex items-end sm:items-center justify-center p-4">
      <div className="w-full max-w-lg rounded-2xl bg-cream border border-ink/10 shadow-xl p-5 max-h-[92vh] overflow-y-auto">
        <div className="flex items-center gap-2">
          <span className="relative flex size-2.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-soft opacity-60" />
            <span className="relative inline-flex size-2.5 rounded-full bg-soft" />
          </span>
          <h2 className="font-display font-semibold">Recovery call · {call.phone_masked}</h2>
        </div>
        <p className="text-xs text-ink/50 mt-1">
          You are the customer. Ask questions or decide: the agent answers from the debt&apos;s real facts, and a
          decision goes through the real classifier and router. Questions it cannot answer go to a person.
        </p>

        {/* conversation */}
        <div className="mt-3 space-y-2 max-h-[42vh] overflow-y-auto pr-1">
          {lines.map((l, i) => (
            <div key={i} className={`flex ${l.who === "you" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                l.who === "you" ? "bg-ink text-cream rounded-br-sm" : "bg-white border border-ink/10 rounded-bl-sm"}`}>
                {l.text}
                {l.who === "agent" && (
                  <button type="button" onClick={() => void speak(l.text, l.turn)}
                    className="ml-2 text-xs opacity-60 hover:opacity-100" title="Play this line">🔊</button>
                )}
                {l.who === "agent" && l.timings?.server_ms != null && (
                  <span className="block mt-1 text-[10px] text-ink/40"
                    title={`thinking ${l.timings.llm_ms ?? "?"} ms · listening ${l.timings.stt_ms ?? "?"} ms`}>
                    ⏱ {(l.timings.server_ms / 1000).toFixed(1)}s
                  </span>
                )}
                {l.who === "agent" && l.sources && l.sources.length > 0 && (
                  <span className="block mt-1 text-[10px] text-ink/50"
                    title="Taken from a document the merchant approved; code checked the wording before it was spoken.">
                    📄 {l.kind === "answer_policy_verbatim" ? "Approved text" : "From approved policy"}:{" "}
                    {l.sources.map((s) => s.heading || s.title).join(", ")}
                  </span>
                )}
                {l.who === "agent" && l.kind === "answer_generated" && (
                  <span className="block mt-1 text-[10px] text-ink/40"
                    title="The AI wrote this from the debt's own facts; code checked it before it was spoken.">
                    ✍️ AI-written · checked
                  </span>
                )}
              </div>
            </div>
          ))}
          {busy && <p className="text-xs text-ink/40">…</p>}
          <div ref={endRef} />
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-ink/50">
          <span>{cloudVoice ? "Cloud neural voice" : canSpeak ? "Browser voice" : "No speech available"}</span>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={autoPlay} onChange={(e) => setAutoPlay(e.target.checked)} /> Speak replies
          </label>
          {cloudEar && !handsFreeOn && (
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={autoSend} onChange={(e) => setAutoSend(e.target.checked)} /> Send after I speak
            </label>
          )}
          <span>{customerTurns} of 6 customer lines</span>
        </div>

        {result ? (
          <div className="mt-4 rounded-xl border border-ink/10 bg-white p-3 text-sm space-y-1">
            <p>
              <span className="text-ink/50">Understood as</span> <strong>{result.intent}</strong>{" "}
              <span className="text-xs text-ink/40">
                ({result.intent_source === "llm" ? "LLM" : result.intent_source === "handoff" ? "handed to a person" : "keyword fallback"})
              </span>
            </p>
            {result.promise_date && (
              <p>
                <span className="text-ink/50">Date heard</span> {result.promise_date}
              </p>
            )}
            <p
              className={`rounded-md px-2 py-1.5 ${
                result.tone === "ok" ? "bg-soft/10 text-soft" : result.tone === "info" ? "bg-ink/5" : "bg-declined/15 text-ink"
              }`}
            >
              {result.explanation}
            </p>
            <button type="button" onClick={onDone} className="mt-3 rounded-md bg-ink text-cream px-4 py-1.5 text-sm font-semibold">
              Hang up
            </button>
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            {cloudEar && (
              <div className="rounded-lg border border-ink/10 bg-white px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-2">
                    <span className={`size-2.5 rounded-full ${
                      !handsFreeOn ? "bg-ink/25"
                        : live.status === "hearing" ? "bg-ember animate-pulse"
                        : stage !== "idle" ? "bg-amber-400 animate-pulse" : "bg-soft animate-pulse"}`} />
                    {handsFreeOn
                      ? stage === "transcribing" ? "Understanding what you said…"
                        : stage === "thinking" ? "Thinking…"
                        : live.status === "hearing" ? "Hearing you…"
                        : "Listening — just talk, no buttons"
                      : handsFree && live.status === "loading" ? "Starting the microphone…"
                      : handsFree && live.status === "error" ? `Microphone unavailable (${live.error || "blocked"}) — use the buttons`
                      : "Hands-free is off — use the buttons"}
                  </span>
                  <label className="flex items-center gap-1 text-ink/60">
                    <input type="checkbox" checked={handsFree} onChange={(e) => setHandsFree(e.target.checked)} /> Hands-free
                  </label>
                </div>
                {handsFreeOn && (
                  <p className="mt-1 text-[11px] text-ink/40">
                    Use headphones so the agent&apos;s voice is not picked up. You can talk over the agent to interrupt.
                  </p>
                )}
              </div>
            )}
            <div>
              <p className="text-[11px] uppercase tracking-wide text-ink/40 mb-1">Ask something</p>
              <div className="flex flex-wrap gap-1.5">
                {QUICK_REPLIES.filter((q) => q.group === "ask").map((q) => (
                  <button key={q.label} type="button" onClick={() => void submit(q.text)} disabled={busy}
                    className="text-xs px-2.5 py-1 rounded-full border border-ink/15 hover:bg-white disabled:opacity-40">
                    {q.label}
                  </button>
                ))}
              </div>
              <p className="text-[11px] uppercase tracking-wide text-ink/40 mt-2 mb-1">Or decide (ends the call)</p>
              <div className="flex flex-wrap gap-1.5">
                {QUICK_REPLIES.filter((q) => q.group === "decide").map((q) => (
                  <button key={q.label} type="button" onClick={() => void submit(q.text)} disabled={busy}
                    className="text-xs px-2.5 py-1 rounded-full border border-ink/15 hover:bg-white disabled:opacity-40">
                    {q.label}
                  </button>
                ))}
              </div>
            </div>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(text); }
              }}
              rows={2}
              placeholder="Type or speak what the customer says… (Hinglish, Hindi or English)"
              className="w-full rounded-md border border-ink/15 px-3 py-2 text-sm bg-white"
            />
            <div className="flex flex-wrap gap-2">
              {handsFreeOn ? null : cloudEar ? (
                <button type="button" onClick={toggleRecord} disabled={transcribing || busy}
                  className="text-sm px-3 py-1.5 rounded-md border border-ink/15 disabled:opacity-40"
                  title="Records your voice and transcribes it with Whisper">
                  {transcribing ? "Transcribing…" : recording ? "⏹ Stop recording" : "🎙 Speak"}
                </button>
              ) : (
                <button type="button" onClick={listen} disabled={!Recog || listening}
                  className="text-sm px-3 py-1.5 rounded-md border border-ink/15 disabled:opacity-40"
                  title={Recog ? "" : "Speech recognition is not available in this browser"}>
                  {listening ? "Listening…" : "🎙 Speak"}
                </button>
              )}
              <button type="button" onClick={() => void submit(text)} disabled={!text.trim() || busy}
                className="text-sm px-4 py-1.5 rounded-md bg-ink text-cream font-semibold disabled:opacity-50">
                Send
              </button>
              <button type="button" onClick={() => void noAnswer()} disabled={busy}
                className="text-sm px-3 py-1.5 rounded-md border border-ink/15">
                Hang up without answering
              </button>
            </div>
            {voiceNote && <p className="text-xs text-ember">{voiceNote}</p>}
            {error && <p className="text-sm text-failed">{error}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
