import { createFileRoute } from "@tanstack/react-router";

import { BusinessInvoicesPage } from "@/features/account/BusinessInvoicesPage";

export const Route = createFileRoute("/account/invoices")({
  head: () => ({
    meta: [
      { title: "Your invoices — Backstop" },
      { name: "description", content: "See what you owe, pay an invoice, and choose how we reach you." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: BusinessInvoicesPage,
});
