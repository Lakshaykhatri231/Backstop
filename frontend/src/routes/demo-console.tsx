import { createFileRoute } from "@tanstack/react-router";

import { DemoConsolePage } from "@/features/demo/DemoConsolePage";

export const Route = createFileRoute("/demo-console")({
  head: () => ({
    meta: [
      { title: "Demo Console — Backstop" },
      { name: "description", content: "Watch notices, calls and promises happen: a simulated phone inbox, a call simulator and a fast-forward clock." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: DemoConsolePage,
});
