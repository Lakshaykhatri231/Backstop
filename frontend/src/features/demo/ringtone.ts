// A phone-style ringtone made with the Web Audio API (no audio file to ship).
// Browsers keep audio suspended until the user has interacted with the page,
// so unlockAudio() is called on the first click anywhere in the console; the
// incoming-call screen shows a hint when the ringtone cannot play yet.
let ctx: AudioContext | null = null;
let timer: number | null = null;
let active: { stop: () => void }[] = [];

function getCtx(): AudioContext | null {
  try {
    if (!ctx) {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    return ctx;
  } catch {
    return null;
  }
}

export function unlockAudio(): void {
  const c = getCtx();
  if (c && c.state === "suspended") void c.resume();
}

export function audioReady(): boolean {
  return !!ctx && ctx.state === "running";
}

function burst(c: AudioContext, start: number, length: number) {
  const master = c.createGain();
  master.gain.setValueAtTime(0, start);
  master.gain.linearRampToValueAtTime(0.18, start + 0.02);
  master.gain.setValueAtTime(0.18, start + length - 0.04);
  master.gain.linearRampToValueAtTime(0, start + length);
  master.connect(c.destination);
  // two close tones, modulated, give the familiar "brrr" of a phone
  for (const f of [440, 480]) {
    const o = c.createOscillator();
    o.type = "sine";
    o.frequency.value = f;
    const lfo = c.createOscillator();
    lfo.frequency.value = 22;
    const depth = c.createGain();
    depth.gain.value = 0.35;
    const am = c.createGain();
    am.gain.value = 0.65;
    lfo.connect(depth);
    depth.connect(am.gain);
    o.connect(am);
    am.connect(master);
    o.start(start);
    lfo.start(start);
    o.stop(start + length);
    lfo.stop(start + length);
    active.push({ stop: () => { try { o.stop(); lfo.stop(); } catch { /* already stopped */ } } });
  }
}

function ringOnce() {
  const c = getCtx();
  if (!c || c.state !== "running") return;
  const t = c.currentTime + 0.05;
  burst(c, t, 0.4);
  burst(c, t + 0.6, 0.4);
}

export function startRing(): void {
  stopRing();
  unlockAudio();
  ringOnce();
  timer = window.setInterval(ringOnce, 3000);
  try { navigator.vibrate?.([400, 200, 400, 2000]); } catch { /* not supported */ }
}

export function stopRing(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
  active.forEach((a) => a.stop());
  active = [];
  try { navigator.vibrate?.(0); } catch { /* not supported */ }
}
