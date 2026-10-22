import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getModes, setDebitMode, setLinksMode, type DemoModes } from "@/lib/api/demo";

const DEBIT_OPTIONS: { value: DemoModes["debit"]; label: string; help: string }[] = [
  { value: "real", label: "Ask Razorpay (real)", help: "Makes a real Test Mode debit. It only works close to the time you authorised the card." },
  { value: "success", label: "Play: debit succeeds", help: "No call to Razorpay. A captured payment is played through the real webhook code." },
  { value: "insufficient_funds", label: "Play: debit fails", help: "No call to Razorpay. A failed payment (insufficient funds) is played through the real webhook code." },
];

/** What is real, what is a stand-in, and the switches that decide whether the simulation spends Razorpay Test Mode allowance. */
export function ModesCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["demo", "modes"], queryFn: getModes, refetchInterval: 5000 });
  const done = (m: DemoModes) => { qc.setQueryData(["demo", "modes"], m); void qc.invalidateQueries({ queryKey: ["demo"] }); };
  const links = useMutation({ mutationFn: setLinksMode, onSuccess: done });
  const debit = useMutation({ mutationFn: setDebitMode, onSuccess: done });

  // Test Mode allows about 30 payment links, so the console starts every visit
  // with real links OFF (labelled stand-ins). Turning them on is the person's choice.
  const initial = useRef(false);
  useEffect(() => {
    if (q.data && !initial.current) {
      initial.current = true;
      if (q.data.links_switch === null) links.mutate(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data?.links_switch]);

  const m = q.data;
  if (!m) return <p className="text-sm text-ink/50">Loading…</p>;
  const u = m.links;
  const used = u?.account_links_total ?? u?.real_links_created_here ?? 0;
  const realOn = m.links_switch === true;

  return (
    <div className="space-y-4">
      <div className="rounded-xl bg-sand/60 border border-ink/10 px-4 py-3 text-sm leading-relaxed space-y-1">
        <p><strong>The backend runs exactly as it would live.</strong> Only the clock and the phone line are simulated.</p>
        {m.test_mode ? (
          <p>
            <strong>Razorpay Test Mode limits:</strong> a business can create about {u?.cap ?? 30} Payment Links; card
            autopay works with cards only (no UPI AutoPay); no bank pre-debit message is sent; a debit's result arrives about
            5 minutes after it is made. Razorpay's test environment runs on <em>real</em> time, and its docs state no
            guarantee past a few days - so keep real steps within about {m.window_days} days of each other when you want them
            to work. <strong>Live mode has none of these limits.</strong>
          </p>
        ) : (
          <p>{m.keys_present ? "Razorpay is in LIVE mode." : "No Razorpay keys are configured, so every link is a labelled stand-in."}</p>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="rounded-xl border border-ink/10 p-3 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-sm">Payment links</span>
            <span className={`text-xs px-2 py-0.5 rounded-full ${realOn ? "bg-soft/10 text-soft" : "bg-declined/15 text-declined"}`}>
              {realOn ? "real links ON" : "stand-ins (no limit used)"}
            </span>
          </div>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={realOn} disabled={links.isPending || !m.keys_present}
              onChange={(e) => links.mutate(e.target.checked)} />
            <span>
              Spend real Razorpay Test Mode links in this simulation
              <span className="block text-xs text-ink/50">
                Off (recommended): every link in an SMS is a labelled stand-in, so the whole lifecycle runs without using the allowance.
                The run-to-the-end buttons never use real links, whatever this says.
              </span>
            </span>
          </label>
          {m.test_mode && u && (
            <div>
              <div className="h-2 rounded-full bg-ink/10 overflow-hidden">
                <div className={`h-full ${used >= u.cap ? "bg-failed" : used >= u.cap * 0.8 ? "bg-declined" : "bg-soft"}`}
                  style={{ width: `${Math.min(100, (used / u.cap) * 100)}%` }} />
              </div>
              <p className="text-xs text-ink/60 mt-1">{used} of ~{u.cap} Test Mode links used on the account ({u.real_links_created_here} made by this app).</p>
            </div>
          )}
        </div>

        <div className="rounded-xl border border-ink/10 p-3 space-y-2">
          <span className="font-medium text-sm">Autopay debit result</span>
          {DEBIT_OPTIONS.map((o) => (
            <label key={o.value} className="flex items-start gap-2 text-sm">
              <input type="radio" name="debit-mode" className="mt-1" checked={m.debit === o.value} disabled={debit.isPending}
                onChange={() => debit.mutate(o.value)} />
              <span>{o.label}<span className="block text-xs text-ink/50">{o.help}</span></span>
            </label>
          ))}
          <p className="text-[11px] text-ink/50">Played results are labelled in the audit log and book money through the normal paths.</p>
        </div>
      </div>
    </div>
  );
}
