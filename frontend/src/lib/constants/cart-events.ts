import type { Tone } from "./tone";

// Ported verbatim from static/index.html's CART_EVENT_COLOR/CART_EVENT_LABEL.
export const CART_EVENT_LABEL: Record<string, string> = {
  silent_abandon: "Silent Abandon",
  explicit_cancel: "Explicit Cancel",
};

export const CART_EVENT_TONE: Record<string, Tone> = {
  silent_abandon: "loyal",
  explicit_cancel: "declined",
};
