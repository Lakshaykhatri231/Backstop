import { createFileRoute, Outlet, useLocation } from "@tanstack/react-router";

import { AutopayPage } from "@/features/account/AutopayPage";

export const Route = createFileRoute("/account")({
  head: () => ({
    meta: [
      { title: "Your autopay — Backstop" },
      { name: "description", content: "Start autopay for a service, choose how we reach you, and see your next debit." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: AccountRoute,
});

// /account is the customer's autopay page; /account/invoices (a business
// customer) renders through the Outlet instead.
function AccountRoute() {
  const path = useLocation({ select: (l) => l.pathname });
  return path === "/account" || path === "/account/" ? <AutopayPage /> : <Outlet />;
}
