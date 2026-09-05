import { createFileRoute } from "@tanstack/react-router";

import { TiersPage } from "@/features/tiers/TiersPage";

export const Route = createFileRoute("/tiers")({
  head: () => ({
    meta: [
      { title: "Customer tiers — Backstop recovery agent" },
      {
        name: "description",
        content:
          "How Backstop tiers customers: the five tiers, the 0-100 engagement-score formula behind them, how the tier decision is made, and the benefits, discount bands and frequency caps each tier gets.",
      },
    ],
  }),
  component: TiersPage,
});
