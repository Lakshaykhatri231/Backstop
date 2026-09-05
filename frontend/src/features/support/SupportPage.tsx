import { Link } from "@tanstack/react-router";

export function SupportPage() {
  return (
    <div className="min-h-screen w-full grid place-items-center bg-cream text-ink font-body p-6">
      <div className="w-full max-w-md rounded-2xl bg-white border border-ink/5 p-9 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
        <div className="text-3xl mb-3">💬</div>
        <h1 className="font-display text-xl font-bold mb-2">We're here to help</h1>
        <p className="text-sm text-ink/50 leading-relaxed mb-6">
          Having trouble completing a payment? Reach out and we'll help sort it out.
        </p>

        <div className="rounded-xl bg-cream border border-ink/10 px-4 py-3.5 mb-3">
          <div className="text-[10.5px] uppercase tracking-wide text-ink/40">Email</div>
          <a
            href="mailto:support@retain-demo.com?subject=Payment%20issue"
            className="text-sm font-semibold text-ink hover:text-brand"
          >
            support@retain-demo.com
          </a>
        </div>
        <div className="rounded-xl bg-cream border border-ink/10 px-4 py-3.5 mb-3">
          <div className="text-[10.5px] uppercase tracking-wide text-ink/40">Phone</div>
          <a href="tel:+918800000000" className="text-sm font-semibold text-ink hover:text-brand">
            +91 88000 00000
          </a>
        </div>

        <p className="text-xs text-ink/40 mt-5 text-center">We typically respond within a few hours.</p>
        <div className="text-center mt-6">
          <Link to="/store" className="text-sm font-semibold text-brand hover:underline">
            ← Back to store
          </Link>
        </div>
      </div>
    </div>
  );
}
