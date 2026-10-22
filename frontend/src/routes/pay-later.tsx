import { createFileRoute } from "@tanstack/react-router";

import { PayLaterPage } from "@/features/payLater/PayLaterPage";

type PayLaterSearch = { st: string; sid: string; t: string };

export const Route = createFileRoute("/pay-later")({
  validateSearch: (s: Record<string, unknown>): PayLaterSearch => ({
    st: String(s["st"] ?? ""),
    sid: String(s["sid"] ?? ""),
    t: String(s["t"] ?? ""),
  }),
  head: () => ({
    meta: [
      { title: "Choose when you'll pay — Backstop" },
      { name: "description", content: "Confirm your number and tell us when you'll pay." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: PayLaterRoute,
});

function PayLaterRoute() {
  const { st, sid, t } = Route.useSearch();
  return <PayLaterPage link={{ subject_type: st, subject_id: sid, token: t }} />;
}
