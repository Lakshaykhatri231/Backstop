import type { DemoMessage } from "@/lib/api/demo";

import { fmtIst, TEMPLATE_LABEL } from "./format";

const STATUS_STYLE: Record<DemoMessage["status"], string> = {
  recorded: "bg-soft/10 text-soft",
  sent: "bg-soft/10 text-soft",
  failed: "bg-failed/10 text-failed",
  not_sent_no_contact: "bg-failed/10 text-failed",
};

const STATUS_LABEL: Record<DemoMessage["status"], string> = {
  recorded: "recorded (sandbox)",
  sent: "sent",
  failed: "failed",
  not_sent_no_contact: "not sent: no number",
};

function Bubble({ m }: { m: DemoMessage }) {
  return (
    <div className="max-w-[92%]">
      <div className="rounded-2xl rounded-tl-sm bg-white border border-ink/10 px-3 py-2 text-sm leading-snug shadow-sm">
        <div className="flex items-center gap-2 mb-1 flex-wrap">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-ink/50">
            {TEMPLATE_LABEL[m.template] ?? m.template}
          </span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${STATUS_STYLE[m.status]}`}>
            {STATUS_LABEL[m.status]}
          </span>
        </div>
        <p className="whitespace-pre-wrap break-words">{m.body}</p>
        {m.link_kind === "placeholder" && (
          <p className="mt-1.5 text-[11px] text-declined">
            Stand-in link: no real Razorpay link was made for this (stub mode, no number to send it to, or the
            Test Mode link limit was reached - see the Razorpay links counter).
          </p>
        )}
        {m.error && <p className="mt-1.5 text-[11px] text-failed">Reason: {m.error}</p>}
      </div>
      <div className="text-[10px] text-ink/40 mt-0.5 ml-1">
        {fmtIst(m.created_at)} IST · {m.provider}
        {m.link && m.link_kind === "self_serve" && (
          <>
            {" · "}
            <a className="underline" href={m.link} target="_blank" rel="noreferrer">
              open customer page ↗
            </a>
          </>
        )}
        {m.link && (m.link_kind === "razorpay_payment_link" || m.link_kind === "razorpay_registration_link") && (
          <>
            {" · "}
            <a className="underline" href={m.link} target="_blank" rel="noreferrer">
              {m.link_kind === "razorpay_payment_link" ? "pay on Razorpay (Test Mode) ↗" : "set up autopay on Razorpay ↗"}
            </a>
          </>
        )}
        {m.link && m.link_kind === "storefront" && (
          <>
            {" · "}
            <a className="underline" href={m.link} target="_blank" rel="noreferrer">
              open store ↗
            </a>
          </>
        )}
      </div>
    </div>
  );
}

// "The customer's phone": every text the system sent, grouped by number.
// In sandbox mode this table is the only place a message exists - nothing is
// delivered anywhere else.
export function PhoneInbox({ messages }: { messages: DemoMessage[] }) {
  const groups = new Map<string, DemoMessage[]>();
  for (const m of messages) {
    const key = m.to_phone ?? "No number on file";
    groups.set(key, [...(groups.get(key) ?? []), m]);
  }

  if (messages.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-ink/15 p-8 text-center text-sm text-ink/50">
        No messages yet. Create a scenario, then verify the number or move the clock — texts appear here.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {[...groups.entries()].map(([phone, list]) => (
        <div key={phone} className="rounded-[28px] border-2 border-ink/80 bg-sand/60 overflow-hidden">
          <div className="bg-ink text-cream px-4 py-2 text-xs flex justify-between">
            <span>{phone}</span>
            <span>{list.length} message{list.length === 1 ? "" : "s"}</span>
          </div>
          <div className="p-3 space-y-3 max-h-[420px] overflow-y-auto flex flex-col-reverse">
            {/* newest at the bottom like a chat: list is newest-first, column-reverse flips it */}
            {list.map((m) => (
              <Bubble key={m.id} m={m} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
