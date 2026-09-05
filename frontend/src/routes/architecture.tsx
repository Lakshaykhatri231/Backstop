import { createFileRoute } from "@tanstack/react-router";

import { ArchitecturePage } from "@/features/architecture/ArchitecturePage";

export const Route = createFileRoute("/architecture")({
  head: () => ({
    meta: [
      { title: "Architecture — Backstop recovery agent" },
      {
        name: "description",
        content:
          "A diagrammatic walkthrough of Backstop's internals: the three signal sources, the bounded rules-then-LLM decision pipeline, the revenue ledger, tiering, the self-tuning insight loop, and the hash-chained audit log.",
      },
    ],
  }),
  component: ArchitecturePage,
});
