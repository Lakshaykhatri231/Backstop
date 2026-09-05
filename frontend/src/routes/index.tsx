import { createFileRoute, Link } from "@tanstack/react-router";
import heroTable from "@/assets/hero-table.jpg";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Backstop — Recover failed payments & abandoned carts" },
      {
        name: "description",
        content:
          "Backstop intercepts failed payments, checkout drop-offs and cart abandonment on Razorpay, runs bounded recovery rules with a human gate, and audits every rupee.",
      },
      { property: "og:title", content: "Backstop — Revenue recovery agent for Razorpay" },
      {
        property: "og:description",
        content:
          "Three recovery threads, bounded discount bands, human gating and a hash-chained audit log. Every rupee tracked to exactly one exit.",
      },
    ],
  }),
  component: Home,
});

function Home() {
  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <header className="border-b border-ink/10 bg-cream/80">
        <div className="mx-auto max-w-[1320px] px-6 h-16 flex items-center gap-8">
          <div className="flex items-center gap-2.5">
            <span className="font-display font-semibold text-lg tracking-tight">Backstop</span>
          </div>
          <nav className="hidden md:flex items-center gap-1 text-sm">
            <Link to="/dashboard" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Dashboard
            </Link>
            <Link to="/store" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Storefront
            </Link>
            <Link to="/architecture" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Architecture
            </Link>
            <Link to="/tiers" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Tiers
            </Link>
            <Link to="/login" className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink">
              Sign in
            </Link>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden sm:flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-soft/10 text-soft">
              <span className="size-1.5 rounded-full bg-soft" /> Razorpay Test Mode
            </span>
          </div>
        </div>
      </header>

      <section className="relative overflow-hidden">
        <div className="absolute inset-0 -z-10 bg-gradient-to-br from-amber-200 via-orange-100 to-[#fdf3e0]" />
        <div className="mx-auto max-w-[1200px] px-6 grid lg:grid-cols-2 gap-10 items-center py-16">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-ember font-semibold mb-3">
              Revenue protection agent
            </p>
            <h1 className="font-display text-5xl font-semibold tracking-tight leading-[1.05]">
              Every rupee at risk, tracked to exactly one exit.
            </h1>
            <p className="text-ink/70 mt-4 max-w-md">
              Backstop intercepts failed payments, checkout drop-offs and pre-checkout abandonment,
              runs bounded per-tier discount rules, gates the risky cases to a human, and writes
              every state change to a hash-chained audit log.
            </p>
            <div className="flex items-center gap-3 mt-7">
              <Link
                to="/dashboard"
                className="px-6 py-3 rounded-lg bg-ink text-cream font-semibold shadow-[0_18px_34px_-16px_rgba(43,29,18,0.7)]"
              >
                Open the console
              </Link>
              <Link
                to="/store"
                className="px-6 py-3 rounded-lg bg-cream/70 text-ink font-medium border border-ink/10"
              >
                Try the demo store
              </Link>
            </div>
          </div>
          <img
            src={heroTable}
            width={1024}
            height={1280}
            alt="Glazed stoneware mugs and bowls on a linen table in golden-hour light"
            className="aspect-[4/5] w-full object-cover rounded-3xl shadow-[0_40px_80px_-40px_rgba(234,88,12,0.5)]"
          />
        </div>
      </section>

      <section className="mx-auto max-w-[1200px] px-6 py-14">
        <h2 className="font-display text-2xl font-semibold tracking-tight mb-6">
          Three threads — soft, declined, failed
        </h2>
        <div className="grid sm:grid-cols-3 gap-5">
          <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.5)]">
            <span className="size-2.5 rounded-full bg-soft inline-block" />
            <p className="font-display font-semibold text-lg mt-3">Soft</p>
            <p className="text-sm text-ink/60 mt-2">
              Pre-checkout abandonment and cancellation. Nudges and tier-sized incentives before the
              cart cools.
            </p>
          </div>
          <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.5)]">
            <span className="size-2.5 rounded-full bg-declined inline-block" />
            <p className="font-display font-semibold text-lg mt-3">Declined</p>
            <p className="text-sm text-ink/60 mt-2">
              Checkout drop-off. Retry windows, alternate methods, bounded discount bands per tier.
            </p>
          </div>
          <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.5)]">
            <span className="size-2.5 rounded-full bg-failed inline-block" />
            <p className="font-display font-semibold text-lg mt-3">Failed</p>
            <p className="text-sm text-ink/60 mt-2">
              Gateway and bank failures. Low-confidence cases hand off to a human instead of
              guessing.
            </p>
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-[1200px] px-6 pb-16">
        <div className="rounded-2xl bg-gradient-to-br from-ink to-[#4a2c12] text-cream p-8 shadow-[0_28px_56px_-24px_rgba(43,29,18,0.6)]">
          <div className="flex items-center gap-2 mb-3">
            <span className="size-6 rounded-md bg-tangerine/20 grid place-items-center text-tangerine text-xs font-bold">
              AI
            </span>
            <h2 className="font-display font-semibold text-lg">Insight layer</h2>
          </div>
          <p className="text-cream/85 text-sm leading-relaxed max-w-2xl">
            The rules engine stays deterministic and bounded. The model only refines within those
            bounds — then explains policy performance in plain language and proposes configuration
            changes you can apply in one click.
          </p>
          <div className="flex flex-wrap items-center gap-2 mt-6">
            <Link
              to="/dashboard"
              className="px-4 py-2 rounded-lg bg-tangerine text-ink text-sm font-semibold shadow-[0_10px_24px_-8px_rgba(245,158,11,0.8)]"
            >
              See it on live data
            </Link>
            <span className="text-xs text-cream/50">
              Append-only audit log · deterministic bounds enforced
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
