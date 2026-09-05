import {
  ArrowMarker,
  Connector,
  DecisionDiamond,
  ProcessBox,
  SvgLabel,
  Terminal,
  measureProcessBox,
  wrapLabel,
} from "./svgflow";
import type { FlowTone } from "./tokens";

/** The decision-pipeline flowchart, same precision as journey-diagram.tsx.
 *
 * Layout: a shared trunk (event happened -> which source?) forks three ways. The webhook and
 * dropoff sources each get their own short "record" step, but from there the rules engine ->
 * LLM -> gate -> execute -> audit shape is genuinely identical between them — so instead of
 * drawing it twice, both record steps converge into ONE wide shared flowchart, with the two
 * real differences (which rules function runs, and the retry_now exemption) called out
 * explicitly at the exact points where they apply. The storefront cart-event thread stays its
 * own separate, shorter column — it never touches this shared machinery at all.
 *
 * The "LLM vs rules' pick?" diamond is NOT a real code branch — app/llm_agent.py's _run_llm
 * never compares its output to the rules engine's suggestion; on success it returns whatever
 * the LLM said, unconditionally. This diamond honestly describes the *range of outcomes* the
 * prompt's contract ("confirm it or adjust confidence/reasoning; you may NOT choose an action
 * outside the schema... but nothing stops a different in-schema action either") makes possible,
 * not something the code itself checks. */

const TW = 1712; // matches the 1760px page breakout (1712 = content width after padding)
const GAP = 32; // matches the grid's gap-8
const SHARED_W = Math.round(((TW - GAP) * 2) / 3); // grid-cols-[2fr_1fr]'s 2fr share
const STOREFRONT_W = TW - GAP - SHARED_W; // ...and its 1fr share

// Trunk fork targets, in the trunk's own TW-wide coordinate space — computed from the same
// 2fr/1fr split the CSS grid below uses, so the arrows land exactly where their target starts.
const WEBHOOK_FORK_X = SHARED_W * 0.25;
const DROPOFF_FORK_X = SHARED_W * 0.75;
const STOREFRONT_FORK_X = SHARED_W + GAP + STOREFRONT_W / 2;

const TONE_FILL_TEXT: Record<FlowTone, string> = {
  soft: "fill-soft",
  declined: "fill-declined",
  failed: "fill-failed",
  indigo: "fill-indigo",
  loyal: "fill-loyal",
  brand: "fill-brand",
  ink: "fill-ink",
};

export function PipelineTrunk() {
  let y = 24;
  const startCy = y + 16;
  y = startCy + 16;
  y += 34;

  const halfW = 150;
  const halfH = 58;
  const diamondCy = y + halfH;
  const diamondBottom = diamondCy + halfH;

  const exitT = 0.42;
  const exitDX = halfW * exitT;
  const exitDY = halfH * (1 - exitT);
  const leftExitX = TW / 2 - exitDX;
  const rightExitX = TW / 2 + exitDX;
  const exitY = diamondCy + exitDY;

  const bendY = diamondBottom + 30;
  const forkY = bendY + 130;

  const totalH = forkY + 10;

  return (
    <svg
      viewBox={`0 0 ${TW} ${totalH}`}
      className="w-full"
      role="img"
      aria-label="An event happens, then the pipeline forks by which of the three sources detected it."
    >
      <defs>
        <ArrowMarker id="pl-arrow-ink" className="fill-ink/35" />
        <ArrowMarker id="pl-arrow-failed" className="fill-failed" />
        <ArrowMarker id="pl-arrow-declined" className="fill-declined" />
        <ArrowMarker id="pl-arrow-soft" className="fill-soft" />
      </defs>

      <Terminal cx={TW / 2} cy={startCy} label="Revenue Leak" tone="start" w={160} />
      <Connector
        points={[
          { x: TW / 2, y: startCy + 16 },
          { x: TW / 2, y: diamondCy - halfH },
        ]}
        markerId="pl-arrow-ink"
        className="stroke-ink/35"
      />

      <DecisionDiamond
        cx={TW / 2}
        cy={diamondCy}
        halfW={halfW}
        halfH={halfH}
        label="Which source detected it?"
        tone="ink"
      />

      <Connector
        points={[
          { x: leftExitX, y: exitY },
          { x: leftExitX, y: bendY },
          { x: WEBHOOK_FORK_X, y: bendY },
          { x: WEBHOOK_FORK_X, y: forkY },
        ]}
        markerId="pl-arrow-failed"
        className="stroke-failed"
        label="payment failure"
        labelAt={{ x: WEBHOOK_FORK_X, y: bendY - 12 }}
      />
      <Connector
        points={[
          { x: TW / 2, y: diamondBottom },
          { x: TW / 2, y: bendY },
          { x: DROPOFF_FORK_X, y: bendY },
          { x: DROPOFF_FORK_X, y: forkY },
        ]}
        markerId="pl-arrow-declined"
        className="stroke-declined"
        label="checkout stuck at attempted"
        labelAt={{ x: DROPOFF_FORK_X, y: (bendY + forkY) / 2 }}
      />
      <Connector
        points={[
          { x: rightExitX, y: exitY },
          { x: rightExitX, y: bendY },
          { x: STOREFRONT_FORK_X, y: bendY },
          { x: STOREFRONT_FORK_X, y: forkY },
        ]}
        markerId="pl-arrow-soft"
        className="stroke-soft"
        label="silent_abandon / explicit_cancel"
        labelAt={{ x: STOREFRONT_FORK_X, y: bendY - 12 }}
        maxChars={22}
      />
    </svg>
  );
}

/** Small in-SVG column header (numbered badge + label) — used inside the shared-flow diagram
 * instead of the page's HTML ColumnHeader, since two of these need to sit side by side above
 * one wide SVG rather than each owning their own. */
function SvgColumnHeader({
  cx,
  cy,
  tone,
  index,
  label,
}: {
  cx: number;
  cy: number;
  tone: FlowTone;
  index: number;
  label: string;
}) {
  return (
    <g>
      <circle cx={cx - 70} cy={cy} r={13} className={TONE_FILL_TEXT[tone]} />
      <text x={cx - 70} y={cy + 4} textAnchor="middle" className="fill-cream text-[12px] font-bold">
        {index}
      </text>
      <text x={cx - 50} y={cy + 5} className="fill-ink text-[16px] font-bold font-display">
        {label}
      </text>
    </g>
  );
}

export function PipelineColumnStorefront() {
  const W = STOREFRONT_W;
  const CX = W / 2;
  const BOX_W = Math.min(340, W - 40);
  const BOX_HALF = BOX_W / 2;
  const marker = "pl-col-c";

  let y = 14;
  const h1 = measureProcessBox("CartEvent row created", "silent_abandon or explicit_cancel.");
  const y1 = y;
  y += h1 + 22;

  const h2 = measureProcessBox(
    "Rules engine picks action, confidence, reasoning",
    "rule_based_cart_event_decision() — fully deterministic.",
  );
  const y2 = y;
  y += h2 + 26;

  const h3 = measureProcessBox("No LLM layer", "Rules only — deliberately no model in the loop.");
  const y3 = y;
  y += h3 + 22;

  const h4 = measureProcessBox(
    "No gate",
    "Nothing to gate — the action is already bounded by the tier formula.",
  );
  const y4 = y;
  y += h4 + 26;

  const h5 = measureProcessBox("Audited", "cart_event_detected, then cart_event_action_executed.");
  const y5 = y;
  y += h5;

  const totalH = y + 20;

  return (
    <svg
      viewBox={`0 0 ${W} ${totalH}`}
      className="w-full"
      role="img"
      aria-label="Storefront cart-event flowchart"
    >
      <defs>
        <ArrowMarker id={marker} className="fill-soft" />
      </defs>
      <ProcessBox
        x={CX - BOX_HALF}
        y={y1}
        w={BOX_W}
        h={h1}
        label="CartEvent row created"
        sub="silent_abandon or explicit_cancel."
        tone="soft"
      />
      <Connector
        points={[
          { x: CX, y: y1 + h1 },
          { x: CX, y: y2 },
        ]}
        markerId={marker}
        className="stroke-soft/60"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y2}
        w={BOX_W}
        h={h2}
        label="Rules engine picks action, confidence, reasoning"
        sub="rule_based_cart_event_decision() — fully deterministic."
        tone="soft"
      />
      <Connector
        points={[
          { x: CX, y: y2 + h2 },
          { x: CX, y: y3 },
        ]}
        markerId={marker}
        className="stroke-soft/60"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y3}
        w={BOX_W}
        h={h3}
        label="No LLM layer"
        sub="Rules only — deliberately no model in the loop."
        dashed
      />
      <Connector
        points={[
          { x: CX, y: y3 + h3 },
          { x: CX, y: y4 },
        ]}
        markerId={marker}
        className="stroke-soft/60"
        dashed
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y4}
        w={BOX_W}
        h={h4}
        label="No gate"
        sub="Nothing to gate — the action is already bounded by the tier formula."
        dashed
      />
      <Connector
        points={[
          { x: CX, y: y4 + h4 },
          { x: CX, y: y5 },
        ]}
        markerId={marker}
        className="stroke-soft/60"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y5}
        w={BOX_W}
        h={h5}
        label="Audited"
        sub="cart_event_detected, then cart_event_action_executed."
        tone="ink"
      />
    </svg>
  );
}

/** The wide, shared rules -> LLM -> gate -> execute -> audit flowchart. Both the payment-webhook
 * and checkout-dropoff sources feed into this one diagram — the only two places their behavior
 * actually differs (which rules function runs, and the retry_now gate exemption) are called out
 * explicitly, right where they apply, instead of the whole shape being drawn twice. */
export function PipelineSharedFlow() {
  const W = SHARED_W;
  const CX = W / 2;
  const BOX_W = 480;
  const BOX_HALF = BOX_W / 2;
  const RECORD_W = 380;
  const RECORD_HALF = RECORD_W / 2;
  const webhookCx = W * 0.25;
  const dropoffCx = W * 0.75;

  const headerY = 22;
  let y = 54;

  const hw = measureProcessBox(
    "Event + Decision row created",
    "Something Razorpay itself told us about.",
  );
  const hd = measureProcessBox(
    "Event + Decision row created",
    "Synthesized from order state, not a webhook payload.",
  );
  const recordY = y;
  const recordBottom = recordY + Math.max(hw, hd);
  y = recordBottom + 46;

  const hRules = measureProcessBox(
    "Rules engine picks action, confidence, reasoning",
    "rule_based_decision() for payment webhooks; rule_based_dropoff_decision() for checkout drop-offs.",
  );
  const rulesY = y;
  y += hRules + 40;

  const hLlm = measureProcessBox("Sent to the LLM", "Groq, function-calling, 8s timeout.");
  const llmY = y;
  y += hLlm + 44;

  const halfW1 = 118;
  const halfH1 = 58;
  const d1Cy = y + halfH1;
  const d1Bottom = d1Cy + halfH1;

  y = d1Bottom + 40;
  const halfW2 = 235;
  const halfH2 = 92;
  const d2Cy = y + halfH2;
  const d2Bottom = d2Cy + halfH2;

  y = d2Bottom + 96;
  const halfW3 = 215;
  const halfH3 = 86;
  const d3Cy = y + halfH3;
  const d3Bottom = d3Cy + halfH3;

  // The gate diamond narrows to a point at its top vertex, so a line landing at some x-offset
  // from center only actually touches the diamond's slanted edge at the y-depth proportional to
  // that offset — a fixed y (like the old gateLandTop) either strands off-center lines short of
  // the edge (too shallow) or drives the center line past the tip and into the fill (too deep).
  const gateTopVertexY = d3Cy - halfH3;
  const gateEdgeY = (xOffsetFromCenter: number) =>
    gateTopVertexY + (Math.abs(xOffsetFromCenter) * halfH3) / halfW3;
  const marginX = 30;
  const landConfirms = CX - 170;
  const landAdjusts = CX;
  const landDifferent = CX + 170;
  const gateLandConfirms = gateEdgeY(CX - landConfirms);
  const gateLandAdjusts = gateEdgeY(CX - landAdjusts);
  const gateLandDifferent = gateEdgeY(CX - landDifferent);

  const gateNoteLines = [
    "Always exempts escalate_to_human.",
    "Also exempts retry_now — but only for",
    "payment-webhook decisions (checkout",
    "drop-off has no retry_now action at all).",
  ];

  const rowY = d3Bottom + 46;
  const hExec = measureProcessBox("execute_action() runs the chosen action");
  const hEsc = measureProcessBox(
    "Force-overridden to escalate_to_human",
    "Still runs through execute_action() and gets audited like any other action.",
  );
  const rowBottom = rowY + Math.max(hExec, hEsc);

  const hAudit = measureProcessBox(
    "Audited",
    "decision_made, then action_executed — identical action_type names for both sources.",
  );
  const termCy = rowBottom + 40 + hAudit / 2;

  const totalH = termCy + hAudit / 2 + 24;

  return (
    <svg
      viewBox={`0 0 ${W} ${totalH}`}
      className="w-full"
      role="img"
      aria-label="Shared decision pipeline for payment webhooks and checkout drop-offs"
    >
      <defs>
        <ArrowMarker id="pl-shared-failed" className="fill-failed" />
        <ArrowMarker id="pl-shared-declined" className="fill-declined" />
        <ArrowMarker id="pl-shared-brand" className="fill-brand" />
        <ArrowMarker id="pl-shared-indigo" className="fill-indigo" />
        <ArrowMarker id="pl-shared-ink" className="fill-ink/30" />
      </defs>

      <SvgColumnHeader
        cx={webhookCx}
        cy={headerY}
        tone="failed"
        index={1}
        label="Payment webhook"
      />
      <SvgColumnHeader
        cx={dropoffCx}
        cy={headerY}
        tone="declined"
        index={2}
        label="Background poller"
      />

      <ProcessBox
        x={webhookCx - RECORD_HALF}
        y={recordY}
        w={RECORD_W}
        h={hw}
        label="Event + Decision row created"
        sub="Something Razorpay itself told us about."
        tone="failed"
      />
      <ProcessBox
        x={dropoffCx - RECORD_HALF}
        y={recordY}
        w={RECORD_W}
        h={hd}
        label="Event + Decision row created"
        sub="Synthesized from order state, not a webhook payload."
        tone="declined"
      />

      {/* both converge on the shared rules-engine box below */}
      <Connector
        points={[
          { x: webhookCx, y: recordY + hw },
          { x: webhookCx, y: recordBottom + 22 },
          { x: CX, y: recordBottom + 22 },
          { x: CX, y: rulesY },
        ]}
        markerId="pl-shared-failed"
        className="stroke-failed"
      />
      <Connector
        points={[
          { x: dropoffCx, y: recordY + hd },
          { x: dropoffCx, y: recordBottom + 22 },
          { x: CX, y: recordBottom + 22 },
          { x: CX, y: rulesY },
        ]}
        markerId="pl-shared-declined"
        className="stroke-declined"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={rulesY}
        w={BOX_W}
        h={hRules}
        label="Rules engine picks action, confidence, reasoning"
        sub="rule_based_decision() for payment webhooks; rule_based_dropoff_decision() for checkout drop-offs."
        tone="brand"
      />
      <Connector
        points={[
          { x: CX, y: rulesY + hRules },
          { x: CX, y: llmY },
        ]}
        markerId="pl-shared-brand"
        className="stroke-brand/60"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={llmY}
        w={BOX_W}
        h={hLlm}
        label="Sent to the LLM"
        sub="Groq, function-calling, 8s timeout."
        tone="indigo"
      />
      <Connector
        points={[
          { x: CX, y: llmY + hLlm },
          { x: CX, y: d1Cy - halfH1 },
        ]}
        markerId="pl-shared-indigo"
        className="stroke-indigo/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={d1Cy}
        halfW={halfW1}
        halfH={halfH1}
        label="LLM call succeeds?"
        tone="indigo"
      />

      <Connector
        points={[
          { x: CX - halfW1, y: d1Cy },
          { x: marginX, y: d1Cy },
          { x: marginX, y: d3Cy },
          { x: CX - halfW3, y: d3Cy },
        ]}
        markerId="pl-shared-ink"
        className="stroke-ink/35"
        dashed
        label="fails — rules' pick, unchanged"
        labelAt={{ x: marginX + 10, y: (d1Cy + d3Cy) / 2, anchor: "start" }}
        labelClassName="fill-ink/45"
        maxChars={20}
      />

      <Connector
        points={[
          { x: CX, y: d1Bottom },
          { x: CX, y: d2Cy - halfH2 },
        ]}
        markerId="pl-shared-indigo"
        className="stroke-indigo/60"
        label="yes"
        labelAt={{ x: CX + 16, y: d1Bottom + 16 }}
        labelClassName="fill-indigo"
      />

      <DecisionDiamond
        cx={CX}
        cy={d2Cy}
        halfW={halfW2}
        halfH={halfH2}
        label="LLM vs rules' pick?"
        tone="indigo"
      />

      {/* Both side exits drop straight down from the vertex first (staying outside the diamond,
          which is widest exactly at vertex height) before cutting inward toward their landing
          x — a direct elbow from the vertex would clip back through the diamond's own fill. */}
      <Connector
        points={[
          { x: CX - halfW2, y: d2Cy },
          { x: CX - halfW2, y: d2Bottom + 16 },
          { x: landConfirms, y: d2Bottom + 16 },
          { x: landConfirms, y: gateLandConfirms },
        ]}
        markerId="pl-shared-indigo"
        className="stroke-indigo/60"
        label="confirms exactly"
        labelAt={{ x: landConfirms, y: d2Bottom + 30 }}
        labelClassName="fill-indigo"
      />
      <Connector
        points={[
          { x: CX, y: d2Bottom },
          { x: landAdjusts, y: gateLandAdjusts },
        ]}
        markerId="pl-shared-indigo"
        className="stroke-indigo"
        label="adjusts confidence/reasoning"
        labelAt={{ x: landAdjusts, y: d2Bottom + 26 }}
        labelClassName="fill-indigo"
        maxChars={20}
      />
      <Connector
        points={[
          { x: CX + halfW2, y: d2Cy },
          { x: CX + halfW2, y: d2Bottom + 16 },
          { x: landDifferent, y: d2Bottom + 16 },
          { x: landDifferent, y: gateLandDifferent },
        ]}
        markerId="pl-shared-indigo"
        className="stroke-indigo/40"
        dashed
        label="different action (rare)"
        labelAt={{ x: landDifferent, y: d2Bottom + 30 }}
        labelClassName="fill-indigo"
        maxChars={15}
      />

      <DecisionDiamond
        cx={CX}
        cy={d3Cy}
        halfW={halfW3}
        halfH={halfH3}
        label="Confidence ≥ threshold?"
        tone="declined"
      />
      <SvgLabel
        x={CX + halfW3 + 26}
        y={d3Cy - 12}
        lines={gateNoteLines}
        anchor="start"
        lineHeight={15}
        className="fill-ink/55 text-[11.5px] font-medium"
      />

      <Connector
        points={[
          { x: CX - 36, y: d3Cy + halfH3 - 14 },
          { x: CX - 36, y: rowY - 14 },
          { x: CX - BOX_HALF + 60, y: rowY - 14 },
          { x: CX - BOX_HALF + 60, y: rowY },
        ]}
        markerId="pl-shared-ink"
        className="stroke-ink/30"
        label="passes, or exempt"
        labelAt={{ x: CX - BOX_HALF + 60, y: rowY - 28 }}
        labelClassName="fill-ink/45"
      />
      <Connector
        points={[
          { x: CX + 36, y: d3Cy + halfH3 - 14 },
          { x: CX + 36, y: rowY - 14 },
          { x: CX + BOX_HALF - 60, y: rowY - 14 },
          { x: CX + BOX_HALF - 60, y: rowY },
        ]}
        markerId="pl-shared-ink"
        className="stroke-ink/30"
        dashed
        label="below threshold"
        labelAt={{ x: CX + BOX_HALF - 60, y: rowY - 28 }}
        labelClassName="fill-ink/45"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={rowY}
        w={BOX_HALF - 30}
        h={hExec}
        label="execute_action() runs the chosen action"
        tone="brand"
      />
      <ProcessBox
        x={CX + 30}
        y={rowY}
        w={BOX_HALF - 30}
        h={hEsc}
        label="Force-overridden to escalate_to_human"
        sub="Still runs through execute_action() and gets audited like any other action."
        tone="declined"
      />

      <Connector
        points={[
          { x: CX - BOX_HALF + 60, y: rowY + hExec },
          { x: CX - BOX_HALF + 60, y: termCy - hAudit / 2 - 18 },
          { x: CX, y: termCy - hAudit / 2 - 18 },
          { x: CX, y: termCy - hAudit / 2 },
        ]}
        markerId="pl-shared-ink"
        className="stroke-ink/30"
      />
      <Connector
        points={[
          { x: CX + BOX_HALF - 60, y: rowY + hEsc },
          { x: CX + BOX_HALF - 60, y: termCy - hAudit / 2 - 18 },
          { x: CX, y: termCy - hAudit / 2 - 18 },
          { x: CX, y: termCy - hAudit / 2 },
        ]}
        markerId="pl-shared-ink"
        className="stroke-ink/30"
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={termCy - hAudit / 2}
        w={BOX_W}
        h={hAudit}
        label="Audited"
        sub="decision_made, then action_executed — identical action_type names for both sources."
        tone="ink"
      />
    </svg>
  );
}
