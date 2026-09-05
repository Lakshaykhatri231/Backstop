import { defineConfig } from "vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import tsConfigPaths from "vite-tsconfig-paths";
import { tanstackRouter } from "@tanstack/router-plugin/vite";

// Plain client-side SPA build — no TanStack Start / SSR / Nitro. FastAPI serves
// the `dist/` output directly (see app/main.py), so the dev proxy below is what
// makes API calls same-origin during development; production is same-origin by
// construction since uvicorn serves both the API and this build.
const BACKEND = "http://localhost:8000";

export default defineConfig({
  plugins: [
    // Must run before viteReact() so file-based routes are generated first.
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    viteReact(),
    tailwindcss(),
    tsConfigPaths(),
  ],
  server: {
    port: 5173,
    proxy: {
      "/auth": BACKEND,
      "/catalog": BACKEND,
      "/cart": BACKEND,
      "/checkout": BACKEND,
      "/merchant": BACKEND,
      "/outcomes": BACKEND,
      "/audit": BACKEND,
      "/insights": BACKEND,
      "/debug": BACKEND,
    },
  },
  build: {
    outDir: "dist",
  },
});
