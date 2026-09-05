import { createFileRoute } from "@tanstack/react-router";

import { DashboardPage } from "@/features/dashboard/DashboardPage";

export const Route = createFileRoute("/dashboard")({
  head: () => ({
    meta: [
      { title: "Merchant Overview — Backstop recovery console" },
      {
        name: "description",
        content:
          "Track at-risk revenue across soft, declined and failed threads, engagement tiers, the human gate queue and LLM policy insights.",
      },
      { property: "og:title", content: "Merchant Overview — Backstop recovery console" },
      {
        property: "og:description",
        content: "At-risk rupees, three conserved recovery threads, tier distribution and one-click policy changes.",
      },
    ],
  }),
  component: DashboardPage,
});
