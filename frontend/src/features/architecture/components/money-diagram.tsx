import {
  ArrowMarker,
  Connector,
  DecisionDiamond,
  ProcessBox,
  Terminal,
  measureProcessBox,
} from "./svgflow";
import type { FlowTone } from "./tokens";

/** "Where the money goes" flowchart — same precision as journey-diagram.tsx and
 * pipeline-diagram.tsx. Three independent lanes, one per at-risk bucket (never crossed) —
 * each books its trigger's full value, then resolves through exactly one of two
 * exits. The lanes share an identical shape but genuinely different mechanics at each step, so
 * unlike the decision pipeline there's nothing to merge here — three parallel columns, not a
 * shared body, is the honest picture of "three separate threads." */

const TW = 1712; // matches the 1760px page breakout, same convention as the other two diagrams
const GAP = 32; // matches the grid's gap-8
const LANE_W = Math.round((TW - 2 * GAP) / 3);
const CX = LANE_W / 2;
const BOX_W = 300;
const BOX_HALF = BOX_W / 2;

const MARKER_STROKE: Record<FlowTone, string> = {
  soft: "stroke-soft",
  declined: "stroke-declined",
  failed: "stroke-failed",
  indigo: "stroke-indigo",
  loyal: "stroke-loyal",
  brand: "stroke-brand",
  ink: "stroke-ink/30",
};

const MARKER_FILL: Record<FlowTone, string> = {
  soft: "fill-soft",
  declined: "fill-declined",
  failed: "fill-failed",
  indigo: "fill-indigo",
  loyal: "fill-loyal",
  brand: "fill-brand",
  ink: "fill-ink/30",
};

// Literal (not template-derived) so Tailwind's JIT scanner actually sees these — see the
// dynamic-class pitfall documented on pipeline-diagram.tsx's own MARKER_FILL/STROKE_60.
const STROKE_60: Record<FlowTone, string> = {
  soft: "stroke-soft/60",
  declined: "stroke-declined/60",
  failed: "stroke-failed/60",
  indigo: "stroke-indigo/60",
  loyal: "stroke-loyal/60",
  brand: "stroke-brand/60",
  ink: "stroke-ink/20",
};

function MoneyLane({
  tone,
  marker,
  bookedSub,
  bucketLabel,
  bucketField,
  recoveredLabel,
  lostLabel,
}: {
  tone: FlowTone;
  marker: string;
  bookedSub: string;
  bucketLabel: string;
  bucketField: string;
  recoveredLabel: string;
  lostLabel: string;
}) {
  let y = 14;

  const h1 = measureProcessBox("Booked at full value", bookedSub);
  const y1 = y;
  y += h1 + 26;

  const h2 = measureProcessBox(bucketLabel, bucketField);
  const y2 = y;
  y += h2 + 34;

  const halfW = 122;
  const halfH = 52;
  const dCy = y + halfH;
  const dBottom = dCy + halfH;

  const rowY = dBottom + 68;
  const exitInset = 34;
  const exitDepth = 12;
  const leftTermCx = CX - 96;
  const rightTermCx = CX + 96;

  const totalH = rowY + 24;

  return (
    <svg
      viewBox={`0 0 ${LANE_W} ${totalH}`}
      className="w-full"
      role="img"
      aria-label={`${bucketLabel} money-flow lane`}
    >
      <defs>
        <ArrowMarker id={marker} className={MARKER_FILL[tone]} />
        <ArrowMarker id={`${marker}-ink`} className="fill-ink/30" />
      </defs>

      <ProcessBox
        x={CX - BOX_HALF}
        y={y1}
        w={BOX_W}
        h={h1}
        label="Booked at full value"
        sub={bookedSub}
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: y1 + h1 },
          { x: CX, y: y2 },
        ]}
        markerId={marker}
        className={STROKE_60[tone]}
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y2}
        w={BOX_W}
        h={h2}
        label={bucketLabel}
        sub={bucketField}
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: y2 + h2 },
          { x: CX, y: dCy - halfH },
        ]}
        markerId={marker}
        className={MARKER_STROKE[tone]}
      />

      <DecisionDiamond
        cx={CX}
        cy={dCy}
        halfW={halfW}
        halfH={halfH}
        label="Recovered or lost?"
        tone={tone}
      />

      <Connector
        points={[
          { x: CX - exitInset, y: dBottom - exitDepth },
          { x: CX - exitInset, y: rowY - 24 },
          { x: leftTermCx, y: rowY - 24 },
          { x: leftTermCx, y: rowY - 17 },
        ]}
        markerId={`${marker}-ink`}
        className="stroke-ink/30"
        label={recoveredLabel}
        labelAt={{ x: leftTermCx, y: rowY - 40 }}
        labelClassName="fill-ink/55"
        maxChars={17}
      />
      <Connector
        points={[
          { x: CX + exitInset, y: dBottom - exitDepth },
          { x: CX + exitInset, y: rowY - 24 },
          { x: rightTermCx, y: rowY - 24 },
          { x: rightTermCx, y: rowY - 17 },
        ]}
        markerId={`${marker}-ink`}
        className="stroke-ink/30"
        dashed
        label={lostLabel}
        labelAt={{ x: rightTermCx, y: rowY - 40 }}
        labelClassName="fill-ink/45"
        maxChars={17}
      />

      <Terminal cx={leftTermCx} cy={rowY} label="Recovered" tone="recovered" />
      <Terminal cx={rightTermCx} cy={rowY} label="Lost" tone="lost" />
    </svg>
  );
}

export function MoneyLaneSilentAbandon() {
  return (
    <MoneyLane
      tone="soft"
      marker="mn-soft"
      bookedSub="A discount offered later never shrinks this figure."
      bucketLabel="At Risk (real recovery targets)"
      bucketField="at_risk_soft"
      recoveredLabel="cart resumes & captures"
      lostLabel="declined/expired, or sweep lapses"
    />
  );
}

export function MoneyLaneExplicitCancel() {
  return (
    <MoneyLane
      tone="declined"
      marker="mn-declined"
      bookedSub="Consolidates every open offer/signal for the customer."
      bucketLabel="At Risk (declined carts)"
      bucketField="at_risk_declined"
      recoveredLabel="cart resumes & captures"
      lostLabel="consolidation shortfall, or lapses"
    />
  );
}

export function MoneyLanePaymentFailure() {
  return (
    <MoneyLane
      tone="failed"
      marker="mn-failed"
      bookedSub="One booking per run — guarded by Order.risk_settled."
      bucketLabel="At Risk (failed payments)"
      bucketField="at_risk_failed"
      recoveredLabel="same/new order captures"
      lostLabel="give-up button, or sweep lapses"
    />
  );
}
