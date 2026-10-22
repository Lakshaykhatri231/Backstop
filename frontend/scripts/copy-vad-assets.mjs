// The browser voice-activity detector (Silero, via @ricky0123/vad-web) loads a
// worklet, a model and an ONNX WebAssembly runtime at run time. Serve them from
// our own origin (public/vad/) so nothing comes from a CDN and the demo works offline.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "public", "vad");
mkdirSync(out, { recursive: true });
const vad = join(root, "node_modules", "@ricky0123", "vad-web", "dist");
const ort = join(root, "node_modules", "onnxruntime-web", "dist");
const files = [
  [join(vad, "vad.worklet.bundle.min.js"), "vad.worklet.bundle.min.js"],
  [join(vad, "silero_vad_v5.onnx"), "silero_vad_v5.onnx"],
  [join(ort, "ort-wasm-simd-threaded.wasm"), "ort-wasm-simd-threaded.wasm"],
  [join(ort, "ort-wasm-simd-threaded.mjs"), "ort-wasm-simd-threaded.mjs"],
];
for (const [src, name] of files) {
  if (!existsSync(src)) {
    console.warn(`[vad assets] missing ${src} - hands-free mode will fall back to push-to-talk`);
    continue;
  }
  copyFileSync(src, join(out, name));
}
console.log("[vad assets] copied to public/vad");
