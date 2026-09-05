import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";

import { AuthCard } from "@/features/auth/AuthCard";

export const Route = createFileRoute("/login")({
  head: () => ({
    meta: [
      { title: "Sign in — Backstop Store" },
      {
        name: "description",
        content: "Sign in to your Backstop Store account to resume your saved cart and offers.",
      },
      { property: "og:title", content: "Sign in — Backstop Store" },
      {
        property: "og:description",
        content: "Sign in to resume your saved cart and any recovery offer waiting for you.",
      },
    ],
  }),
  component: Login,
});

function Login() {
  const navigate = useNavigate();

  return (
    <div className="relative min-h-screen w-full overflow-hidden text-cream font-body">
      <div className="absolute inset-0 -z-10 bg-gradient-to-br from-[#3a1f0a] via-[#7c3f12] to-[#d97706]" />
      <div className="absolute -top-24 -right-24 -z-10 size-96 rounded-full bg-amber-300/40 blur-3xl" />
      <div className="mx-auto max-w-[1200px] px-6 flex min-h-screen items-center">
        <div className="w-full max-w-md">
          <Link to="/" className="flex items-center gap-2.5 mb-8">
            <span className="font-display font-semibold text-xl tracking-tight">Backstop</span>
          </Link>
          <h1 className="font-display text-3xl font-semibold tracking-tight">Welcome back</h1>
          <p className="text-cream/70 text-sm mt-2">
            Sign in to your account to resume your saved cart, or create one to start shopping.
          </p>
          <AuthCard className="mt-8" onAuthed={() => navigate({ to: "/store" })} />
          <p className="text-xs text-cream/50 mt-8 text-center">
            Demo storefront — real Razorpay test-mode checkout.
          </p>
        </div>
      </div>
    </div>
  );
}
