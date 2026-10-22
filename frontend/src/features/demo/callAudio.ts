import { fetchCallAudio, getSpeechConfig } from "@/lib/api/demo";

// While the phone is still ringing, fetch the opening line from the cloud
// voice so that it starts the instant the call is picked up (no wait, and no
// fallback to the browser voice just because the answer was not back yet).
const opening = new Map<string, Promise<Blob | null>>();

export function prefetchOpening(callId: string): void {
  if (opening.has(callId)) return;
  opening.set(
    callId,
    getSpeechConfig()
      .then((cfg) => (cfg.tts === "azure" ? fetchCallAudio(callId) : null))
      .catch(() => null),
  );
}

/** The prefetched opening audio (or null). Resolves only once, then forgets it. */
export async function takeOpening(callId: string): Promise<Blob | null> {
  const p = opening.get(callId);
  opening.delete(callId);
  return p ? p : null;
}
