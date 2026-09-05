import {
  ArrowMarker,
  Connector,
  DecisionDiamond,
  ProcessBox,
  Terminal,
  measureProcessBox,
} from "./svgflow";
import type { FlowTone } from "./tokens";

/** The customer-journey flowchart, precisely laid out: a shared trunk (login -> cart -> 3-way
 * fork) followed by three independently-sized column diagrams, one per thread. Every connector
 * coordinate is derived from the same box/diamond geometry used to draw the shapes, so lines
 * always meet the shape they point at — nothing is eyeballed. Columns share one canvas width so
 * a side-exit terminal next to a diamond always has clear margin either side of the 300-wide
 * process boxes, instead of guessing per-column. */

const W = 650;
const CX = W / 2;
const BOX_W = 300;
const BOX_HALF = BOX_W / 2;

// ---------------------------------------------------------------------------
// Trunk: login -> cart -> 3-way fork
// ---------------------------------------------------------------------------

// Matches the 3-column grid below exactly: same 1712-unit content width (the 1760px breakout
// minus the 24px side padding, twice), same gap-8 (32px) between equal columns — so each fork
// arrow lands dead center over the column it feeds, and the whole trunk spans edge-to-edge like
// the grid instead of floating narrower and centered above it.
const TW = 1712;
const TCX = TW / 2;
const COL_W = (TW - 2 * 32) / 3;
const LEFT_COL_CX = COL_W / 2;
const RIGHT_COL_CX = TW - COL_W / 2;

export function JourneyTrunk() {
  let y = 24;

  const startCy = y + 16;
  y = startCy + 16;
  y += 34; // arrow

  const cartH = measureProcessBox("Adds items to cart");
  const cartY = y;
  y += cartH;
  y += 34; // arrow

  const halfW = 140;
  const halfH = 58;
  const diamondCy = y + halfH;
  const diamondBottom = diamondCy + halfH;

  // Left/right branches exit from real points on the diamond's lower edges (not an arbitrary
  // x offset that happens to sit *inside* the diamond at that height) — a diamond's boundary
  // moves inward as you go down from center, so a fixed offset either starts hidden under the
  // fill or drifts off the edge. Parameterize by t (0 = bottom vertex, 1 = side vertex) instead.
  const exitT = 0.42;
  const exitDX = halfW * exitT;
  const exitDY = halfH * (1 - exitT);
  const leftExitX = TCX - exitDX;
  const rightExitX = TCX + exitDX;
  const exitY = diamondCy + exitDY;

  // A long, clearly-visible drop so each arrow reaches all the way down to just above the
  // column header it feeds — no floating gap between the arrowhead and its target.
  const bendY = diamondBottom + 30;
  const forkY = bendY + 130;
  const leftX = LEFT_COL_CX;
  const midX = TCX;
  const rightX = RIGHT_COL_CX;

  const totalH = forkY + 10;

  return (
    <svg
      viewBox={`0 0 ${TW} ${totalH}`}
      className="w-full"
      role="img"
      aria-label="Customer logs in, adds items to cart, then the journey forks three ways: times out, cancels, or pays."
    >
      <defs>
        <ArrowMarker id="tk-arrow-ink" className="fill-ink/35" />
        <ArrowMarker id="tk-arrow-soft" className="fill-soft" />
        <ArrowMarker id="tk-arrow-declined" className="fill-declined" />
        <ArrowMarker id="tk-arrow-failed" className="fill-failed" />
      </defs>

      <Terminal cx={TCX} cy={startCy} label="Customer logs in" tone="start" w={150} />
      <Connector
        points={[
          { x: TCX, y: startCy + 16 },
          { x: TCX, y: cartY },
        ]}
        markerId="tk-arrow-ink"
        className="stroke-ink/35"
      />

      <ProcessBox x={TCX - 110} y={cartY} w={220} h={cartH} label="Adds items to cart" />
      <Connector
        points={[
          { x: TCX, y: cartY + cartH },
          { x: TCX, y: diamondCy - halfH },
        ]}
        markerId="tk-arrow-ink"
        className="stroke-ink/35"
      />

      <DecisionDiamond
        cx={TCX}
        cy={diamondCy}
        halfW={halfW}
        halfH={halfH}
        label="What happens to the cart?"
        tone="ink"
      />

      <Connector
        points={[
          { x: leftExitX, y: exitY },
          { x: leftExitX, y: bendY },
          { x: leftX, y: bendY },
          { x: leftX, y: forkY },
        ]}
        markerId="tk-arrow-soft"
        className="stroke-soft"
        label="times out"
        labelAt={{ x: leftX, y: bendY - 12 }}
      />
      <Connector
        points={[
          { x: TCX, y: diamondBottom },
          { x: midX, y: forkY },
        ]}
        markerId="tk-arrow-declined"
        className="stroke-declined"
        label="cancels"
        labelAt={{ x: midX + 14, y: (diamondBottom + forkY) / 2, anchor: "start" }}
      />
      <Connector
        points={[
          { x: rightExitX, y: exitY },
          { x: rightExitX, y: bendY },
          { x: rightX, y: bendY },
          { x: rightX, y: forkY },
        ]}
        markerId="tk-arrow-failed"
        className="stroke-failed"
        label="pays"
        labelAt={{ x: rightX, y: bendY - 12 }}
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

/** A decision's two down-and-out exits, landing on terminals side by side below it. */
function ExitPair({
  fromX,
  fromY,
  leftLabel,
  leftTerminal,
  rightLabel,
  rightTerminal,
  markerId,
  toneClass,
}: {
  fromX: number;
  fromY: number;
  leftLabel: string;
  leftTerminal: React.ReactNode;
  rightLabel: string;
  rightTerminal: React.ReactNode;
  markerId: string;
  toneClass: string;
}) {
  const leftX = CX - 90;
  const rightX = CX + 90;
  const midY = fromY + 22;
  const termY = fromY + 62;
  return (
    <g>
      <Connector
        points={[
          { x: fromX, y: fromY },
          { x: fromX, y: midY },
          { x: leftX, y: midY },
          { x: leftX, y: termY - 16 },
        ]}
        markerId={markerId}
        className={toneClass}
        label={leftLabel}
        labelAt={{ x: leftX, y: midY - 8 }}
      />
      <Connector
        points={[
          { x: fromX, y: fromY },
          { x: fromX, y: midY },
          { x: rightX, y: midY },
          { x: rightX, y: termY - 16 },
        ]}
        markerId={markerId}
        className={toneClass}
        label={rightLabel}
        labelAt={{ x: rightX, y: midY - 8 }}
      />
      <g transform={`translate(${leftX}, ${termY})`}>{leftTerminal}</g>
      <g transform={`translate(${rightX}, ${termY})`}>{rightTerminal}</g>
    </g>
  );
}

/** A decision's side exit that terminates immediately, drawn straight out from one vertex —
 * only safe to use when nothing else occupies that row within the terminal's footprint.
 *
 * The line stops at the terminal's facing edge, not its center, so the arrowhead lands visibly
 * next to the pill instead of being drawn underneath it and hidden. The label sits well above
 * the line (clear of both the diamond, which narrows fast away from its vertical center, and
 * the terminal, whose full height only spans +-16 around `cy`) instead of squeezed between them,
 * where it has no room and gets clipped by whichever shape is drawn on top. */
function SideExit({
  diamondCx,
  diamondHalfW,
  cy,
  direction,
  edgeLabel,
  terminal,
  markerId,
  terminalW = 128,
}: {
  diamondCx: number;
  diamondHalfW: number;
  cy: number;
  direction: "left" | "right";
  edgeLabel: string;
  terminal: React.ReactNode;
  markerId: string;
  terminalW?: number;
}) {
  const sign = direction === "right" ? 1 : -1;
  const lineLen = 52;
  const startX = diamondCx + sign * diamondHalfW;
  const lineEndX = startX + sign * lineLen;
  const termCx = lineEndX + sign * (terminalW / 2);
  return (
    <g>
      <Connector
        points={[
          { x: startX, y: cy },
          { x: lineEndX, y: cy },
        ]}
        markerId={markerId}
        className="stroke-ink/30"
        label={edgeLabel}
        labelAt={{ x: (startX + lineEndX) / 2, y: cy - 22 }}
      />
      <g transform={`translate(${termCx}, ${cy})`}>{terminal}</g>
    </g>
  );
}

function ColumnFrame({
  height,
  markerColor,
  markerId,
  children,
}: {
  height: number;
  markerColor: string;
  markerId: string;
  children: React.ReactNode;
}) {
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="w-full" role="img" aria-label="Flowchart branch">
      <defs>
        <ArrowMarker id={markerId} className={markerColor} />
        <ArrowMarker id={`${markerId}-ink`} className="fill-ink/30" />
      </defs>
      {children}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Column A — Timeout (silent abandon)
// ---------------------------------------------------------------------------

export function JourneyColumnTimeout() {
  const tone: FlowTone = "soft";
  const marker = "col-a-arrow";

  let y = 14;
  const h1 = measureProcessBox("Silent abandon recorded", "Cart sits idle past the nudge window.");
  const y1 = y;
  y += h1 + 26;

  const h2 = measureProcessBox(
    "Rules engine sends a nudge or tier discount",
    "Only if the customer's tier is incentive-eligible.",
  );
  const y2 = y;
  y += h2 + 26;

  const halfW = 96;
  const halfH = 52;
  const diamondCy = y + halfH;
  const diamondBottom = diamondCy + halfH;

  const exitFromY = diamondBottom + 8;
  const totalH = exitFromY + 62 + 30;

  return (
    <ColumnFrame height={totalH} markerColor="fill-soft" markerId={marker}>
      <ProcessBox
        x={CX - BOX_HALF}
        y={y1}
        w={BOX_W}
        h={h1}
        label="Silent abandon recorded"
        sub="Cart sits idle past the nudge window."
        tone={tone}
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
        label="Rules engine sends a nudge or tier discount"
        sub="Only if the customer's tier is incentive-eligible."
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: y2 + h2 },
          { x: CX, y: diamondCy - halfH },
        ]}
        markerId={marker}
        className="stroke-soft/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={diamondCy}
        halfW={halfW}
        halfH={halfH}
        label="Resumes in time?"
        tone={tone}
      />

      <ExitPair
        fromX={CX}
        fromY={exitFromY}
        leftLabel="yes"
        leftTerminal={<Terminal cx={0} cy={0} label="Recovered" tone="recovered" />}
        rightLabel="expires"
        rightTerminal={<Terminal cx={0} cy={0} label="Lost" tone="lost" />}
        markerId={`${marker}-ink`}
        toneClass="stroke-ink/30"
      />
    </ColumnFrame>
  );
}

// ---------------------------------------------------------------------------
// Column B — Cancelled (explicit cancel, with cart-snapshot resume + repeat-cancel gate)
// ---------------------------------------------------------------------------

export function JourneyColumnCancelled() {
  const tone: FlowTone = "declined";
  const marker = "col-b-arrow";

  let y = 14;
  const h1 = measureProcessBox(
    "Explicit cancel — cart snapshot saved",
    "items_json captures the cart before it's cleared.",
  );
  const y1 = y;
  y += h1 + 26;

  const halfW1 = 90;
  const halfH1 = 50;
  const d1Cy = y + halfH1;
  const d1Bottom = d1Cy + halfH1;

  y = d1Bottom + 30;
  const h2 = measureProcessBox("Tier incentive-eligible?");
  const halfW2 = 92;
  const halfH2 = 46;
  const d2Cy = y + halfH2;
  const d2Bottom = d2Cy + halfH2;
  void h2;

  y = d2Bottom + 30;
  const h3 = measureProcessBox(
    "Snapshot resurfaces on every /cart visit",
    "Discount if eligible, plain resume link if not — until it expires.",
  );
  const y3 = y;
  y += h3 + 26;

  const halfW3 = 130;
  const halfH3 = 54;
  const d3Cy = y + halfH3;
  const d3Bottom = d3Cy + halfH3;

  const exit3FromY = d3Bottom + 8;
  const totalH = exit3FromY + 62 + 30;

  return (
    <ColumnFrame height={totalH} markerColor="fill-declined" markerId={marker}>
      <ProcessBox
        x={CX - BOX_HALF}
        y={y1}
        w={BOX_W}
        h={h1}
        label="Explicit cancel — cart snapshot saved"
        sub="items_json captures the cart before it's cleared."
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: y1 + h1 },
          { x: CX, y: d1Cy - halfH1 },
        ]}
        markerId={marker}
        className="stroke-declined/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={d1Cy}
        halfW={halfW1}
        halfH={halfH1}
        label="3rd+ cancel?"
        tone={tone}
      />

      <SideExit
        diamondCx={CX}
        diamondHalfW={halfW1}
        cy={d1Cy}
        direction="right"
        edgeLabel="yes"
        terminal={<Terminal cx={0} cy={0} label="Escalated" tone="escalated" />}
        markerId={`${marker}-ink`}
      />

      <Connector
        points={[
          { x: CX, y: d1Bottom },
          { x: CX, y: d2Cy - halfH2 },
        ]}
        markerId={marker}
        className="stroke-declined/60"
        label="no"
        labelAt={{ x: CX + 14, y: d1Bottom + 16 }}
      />

      <DecisionDiamond
        cx={CX}
        cy={d2Cy}
        halfW={halfW2}
        halfH={halfH2}
        label="Tier eligible?"
        tone={tone}
      />

      <SideExit
        diamondCx={CX}
        diamondHalfW={halfW2}
        cy={d2Cy}
        direction="left"
        edgeLabel="NEW / RISK"
        terminal={<Terminal cx={0} cy={0} label="Lost" tone="lost" />}
        markerId={`${marker}-ink`}
      />

      <Connector
        points={[
          { x: CX, y: d2Bottom },
          { x: CX, y: y3 },
        ]}
        markerId={marker}
        className="stroke-declined/60"
        label="eligible"
        labelAt={{ x: CX + 14, y: d2Bottom + 16 }}
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y3}
        w={BOX_W}
        h={h3}
        label="Snapshot resurfaces on every /cart visit"
        sub="Discount if eligible, plain resume link if not — until it expires."
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: y3 + h3 },
          { x: CX, y: d3Cy - halfH3 },
        ]}
        markerId={marker}
        className="stroke-declined/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={d3Cy}
        halfW={halfW3}
        halfH={halfH3}
        label="Acts before expiry?"
        tone={tone}
      />

      <ExitPair
        fromX={CX}
        fromY={exit3FromY}
        leftLabel="resumes"
        leftTerminal={<Terminal cx={0} cy={0} label="Recovered" tone="recovered" />}
        rightLabel="declines"
        rightTerminal={<Terminal cx={0} cy={0} label="Lost" tone="lost" />}
        markerId={`${marker}-ink`}
        toneClass="stroke-ink/30"
      />
    </ColumnFrame>
  );
}

// ---------------------------------------------------------------------------
// Column C — Payment initiated (retry loop)
// ---------------------------------------------------------------------------

export function JourneyColumnPayment() {
  const tone: FlowTone = "failed";
  const marker = "col-c-arrow";

  const y1 = 14;
  const h1 = measureProcessBox(
    "Order created, payment attempted",
    "attempt_count is reconstructed from prior FAILED orders on this basket.",
  );
  const box1Bottom = y1 + h1;
  const box1MidY = y1 + h1 / 2;

  const halfW1 = 90;
  const halfH1 = 52;
  const d1Cy = box1Bottom + 26 + halfH1;
  const d1Bottom = d1Cy + halfH1;

  const h2 = measureProcessBox(
    "Rules engine picks the next action",
    "attempt_count + reason pick retry_now or retry_later; risk_block or a high-value amount escalates immediately.",
  );
  const y2 = d1Bottom + 28;
  const box2Bottom = y2 + h2;

  const halfW2 = 108;
  const halfH2 = 58;
  const d2Cy = box2Bottom + 28 + halfH2;
  const d2Bottom = d2Cy + halfH2;

  const loopMarginL = 30;
  const loopMarginR = W - 30;
  const loopReturnTopY = box1MidY - 16;
  const loopReturnBottomY = box1MidY + 16;

  const escCx = CX - 92;
  const giveCx = CX + 92;
  const termY = d2Bottom + 70;

  const totalH = termY + 30;

  return (
    <ColumnFrame height={totalH} markerColor="fill-failed" markerId={marker}>
      <ProcessBox
        x={CX - BOX_HALF}
        y={y1}
        w={BOX_W}
        h={h1}
        label="Order created, payment attempted"
        sub="attempt_count is reconstructed from prior FAILED orders on this basket."
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: box1Bottom },
          { x: CX, y: d1Cy - halfH1 },
        ]}
        markerId={marker}
        className="stroke-failed/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={d1Cy}
        halfW={halfW1}
        halfH={halfH1}
        label="Gateway result?"
        tone={tone}
      />

      <SideExit
        diamondCx={CX}
        diamondHalfW={halfW1}
        cy={d1Cy}
        direction="right"
        edgeLabel="captured"
        terminal={<Terminal cx={0} cy={0} label="Recovered" tone="recovered" />}
        markerId={`${marker}-ink`}
      />

      <Connector
        points={[
          { x: CX, y: d1Bottom },
          { x: CX, y: y2 },
        ]}
        markerId={marker}
        className="stroke-failed/60"
        label="fails"
        labelAt={{ x: CX + 14, y: d1Bottom + 16 }}
      />

      <ProcessBox
        x={CX - BOX_HALF}
        y={y2}
        w={BOX_W}
        h={h2}
        label="Rules engine picks the next action"
        sub="attempt_count + reason pick retry_now or retry_later; risk_block or a high-value amount escalates immediately."
        tone={tone}
      />
      <Connector
        points={[
          { x: CX, y: box2Bottom },
          { x: CX, y: d2Cy - halfH2 },
        ]}
        markerId={marker}
        className="stroke-failed/60"
      />

      <DecisionDiamond
        cx={CX}
        cy={d2Cy}
        halfW={halfW2}
        halfH={halfH2}
        label="Next action?"
        tone={tone}
      />

      {/* retry_now: exits left vertex, loops up the left margin back into box1's left side */}
      <Connector
        points={[
          { x: CX - halfW2, y: d2Cy },
          { x: loopMarginL, y: d2Cy },
          { x: loopMarginL, y: loopReturnTopY },
          { x: CX - BOX_HALF, y: loopReturnTopY },
        ]}
        markerId={marker}
        className="stroke-failed"
        label="retry_now"
        labelAt={{ x: loopMarginL + 6, y: (d2Cy + loopReturnTopY) / 2, anchor: "start" }}
      />

      {/* retry_later: exits right vertex, loops up the right margin back into box1's right side */}
      <Connector
        points={[
          { x: CX + halfW2, y: d2Cy },
          { x: loopMarginR, y: d2Cy },
          { x: loopMarginR, y: loopReturnBottomY },
          { x: CX + BOX_HALF, y: loopReturnBottomY },
        ]}
        markerId={marker}
        className="stroke-failed/70"
        label="retry_later"
        labelAt={{ x: loopMarginR - 6, y: (d2Cy + loopReturnBottomY) / 2, anchor: "end" }}
      />

      {/* escalate: bottom-left, once both retry_now and retry_later are exhausted */}
      <Connector
        points={[
          { x: CX - 26, y: d2Cy + halfH2 - 14 },
          { x: CX - 26, y: termY - 44 },
          { x: escCx, y: termY - 44 },
          { x: escCx, y: termY - 16 },
        ]}
        markerId={`${marker}-ink`}
        className="stroke-ink/30"
        label="exhausted"
        labelAt={{ x: escCx, y: termY - 52 }}
      />
      <g transform={`translate(${escCx}, ${termY})`}>
        <Terminal cx={0} cy={0} label="Escalated" tone="escalated" />
      </g>

      {/* give up: bottom-right, available any time during the loop */}
      <Connector
        points={[
          { x: CX + 26, y: d2Cy + halfH2 - 14 },
          { x: CX + 26, y: termY - 44 },
          { x: giveCx, y: termY - 44 },
          { x: giveCx, y: termY - 16 },
        ]}
        markerId={`${marker}-ink`}
        className="stroke-ink/30"
        dashed
        label="gives up"
        labelAt={{ x: giveCx, y: termY - 52 }}
      />
      <g transform={`translate(${giveCx}, ${termY})`}>
        <Terminal cx={0} cy={0} label="Lost" tone="lost" />
      </g>
    </ColumnFrame>
  );
}
