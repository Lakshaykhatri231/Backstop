import { createFileRoute } from "@tanstack/react-router";

import { StorePage } from "@/features/store/StorePage";

export const Route = createFileRoute("/store")({
  head: () => ({
    meta: [
      { title: "Store — Backstop" },
      {
        name: "description",
        content: "Demo storefront with real Razorpay Standard Checkout in Test Mode.",
      },
      { property: "og:title", content: "Store — Backstop" },
      {
        property: "og:description",
        content: "Real catalog, real cart, real Razorpay test-mode checkout.",
      },
    ],
  }),
  component: StorePage,
});
