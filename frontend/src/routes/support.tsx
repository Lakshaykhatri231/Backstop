import { createFileRoute } from "@tanstack/react-router";

import { SupportPage } from "@/features/support/SupportPage";

export const Route = createFileRoute("/support")({
  head: () => ({
    meta: [
      { title: "Contact support — Backstop Store" },
      { name: "description", content: "Having trouble completing a payment? Reach out and we'll help sort it out." },
    ],
  }),
  component: SupportPage,
});
