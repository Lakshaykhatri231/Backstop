import { createFileRoute } from "@tanstack/react-router";

import { RegisterInvoicePage } from "@/features/invoices/RegisterInvoicePage";

export const Route = createFileRoute("/invoices")({
  head: () => ({
    meta: [
      { title: "Register an invoice — Backstop" },
      { name: "description", content: "Merchant only: register an invoice for a business customer." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: RegisterInvoicePage,
});
