import { useEffect, useRef, useState } from "react";
import { MicVAD, utils } from "@ricky0123/vad-web";

// Hands-free listening for the call popup. The microphone stays open; Silero
// (a small voice-activity model running in the browser, served from /vad/)
// decides when the customer starts and stops talking, so no button is needed.
//
//  - redemptionMs   silence needed to call it "finished" (library default is 1400)
//  - minSpeechMs    shorter than this is a blip (cough, tap), never sent (250: a quick "haan" / "nahh" is ~300 ms)
//  - preSpeechPadMs audio kept from just before speech began, so the first syllable survives
// While the agent is speaking the bar to count as speech is raised, so its own
// voice leaking into the mic (echo) does not read as the customer.
export type LiveStatus = "off" | "loading" | "listening" | "hearing" | "error";

type Handlers = {
  isAgentSpeaking: () => boolean;
  onSpeechStart: () => void;
  onBargeIn: () => void;
  onUtterance: (wav: Blob, info: { interrupted: boolean }) => void;
  onMisfire: () => void;
};

export function useLiveCall(enabled: boolean, handlers: Handlers, redemptionMs = 800) {
  const [status, setStatus] = useState<LiveStatus>("off");
  const [error, setError] = useState("");
  const h = useRef(handlers);
  h.current = handlers;

  useEffect(() => {
    if (!enabled) {
      setStatus("off");
      return;
    }
    let vad: MicVAD | null = null;
    let cancelled = false;
    let timer: number | null = null;
    let interrupted = false;
    let raised = false;
    setStatus("loading");
    setError("");

    MicVAD.new({
      model: "v5",
      baseAssetPath: "/vad/",
      onnxWASMBasePath: "/vad/",
      redemptionMs,
      preSpeechPadMs: 300,
      minSpeechMs: 250,
      positiveSpeechThreshold: 0.45,
      negativeSpeechThreshold: 0.3,
      startOnLoad: true,
      getStream: () =>
        navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        }),
      onSpeechStart: () => {
        interrupted = h.current.isAgentSpeaking();
        setStatus("hearing");
        h.current.onSpeechStart();
      },
      onSpeechRealStart: () => {
        // Long enough to be real speech: if the agent is talking, stop it now.
        if (h.current.isAgentSpeaking()) {
          interrupted = true;
          h.current.onBargeIn();
        }
      },
      onVADMisfire: () => {
        setStatus("listening");
        h.current.onMisfire();
      },
      onSpeechEnd: (audio: Float32Array) => {
        setStatus("listening");
        const wav = new Blob([utils.encodeWAV(audio, 1, 16000, 1, 16)], { type: "audio/wav" });
        const was = interrupted;
        interrupted = false;
        h.current.onUtterance(wav, { interrupted: was });
      },
    })
      .then((v) => {
        if (cancelled) {
          void v.destroy();
          return;
        }
        vad = v;
        setStatus("listening");
        // raise / lower the bar as the agent starts and stops talking
        timer = window.setInterval(() => {
          const speaking = h.current.isAgentSpeaking();
          if (speaking !== raised && vad) {
            raised = speaking;
            vad.setOptions({ positiveSpeechThreshold: speaking ? 0.7 : 0.45, minSpeechMs: speaking ? 450 : 250 });
          }
        }, 200);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setStatus("error");
        setError(e instanceof Error ? e.message : "Could not start the microphone");
      });

    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
      if (vad) void vad.destroy();
    };
  }, [enabled, redemptionMs]);

  return { status, error };
}

// A reply that ends on a connector or a dangling "kal ya" is probably not finished;
// the call waits a moment for the rest instead of answering half a thought.
const CONTINUATION = /(\b(aur|matlab|toh|to|lekin|par|ya|kyunki|agar|jab|phir|woh|wo|so|and|but|or|because|if|then|the|a)\s*[,.…]*\s*$)|(\.\.\.|…)\s*$/i;
const DEVA_CONTINUATION = /(\s(और|मतलब|तो|लेकिन|पर|या|क्योंकि|अगर|जब|फिर))\s*[,।.…]*\s*$/;

export function looksUnfinished(text: string): boolean {
  const t = text.trim();
  return CONTINUATION.test(t) || DEVA_CONTINUATION.test(t);
}
