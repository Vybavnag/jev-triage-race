import { ms } from "../lib/opponents";
import type { Side } from "../types";
import type { SideState } from "../useRaceStream";

const money = (n: number, known: boolean) => (known ? `$${n.toFixed(4)}` : "unknown");

/** One side of the race: its progress track and measured readouts. Nothing
 * here says who was right; the results table below shows the answers. */
export function Channel({
  side,
  label,
  state,
  total,
}: {
  side: Side;
  label: string;
  state: SideState;
  total: number;
}) {
  const donePct = total ? (state.done / total) * 100 : 0;
  const errPct = total ? (state.errors / total) * 100 : 0;
  return (
    <div className={`card lane-card channel ${side}`}>
      <div className="channel-head">
        <div className="channel-name">
          <span className="lane-chip">{label}</span>
          {state.model && <span className="model">{state.model}</span>}
        </div>
        <div className="num">
          {state.done} / {total}
        </div>
      </div>
      <div className="track">
        <div className="track-row">
          <div className="track-fill" style={{ width: `${donePct - errPct}%` }} />
          <div className="track-errors" style={{ width: `${errPct}%` }} />
        </div>
      </div>
      <dl className="readouts">
        <div className="readout readout--lead">
          <dt>Latest call</dt>
          <dd className="num lead">{ms(state.lastLatency)}</dd>
        </div>
        <div className="readout">
          <dt>Avg latency</dt>
          <dd className="num">{ms(state.totals?.avg_latency_ms ?? null)}</dd>
        </div>
        <div className="readout">
          <dt>Spend</dt>
          <dd className="num">{money(state.costSoFar, state.costKnown)}</dd>
        </div>
        <div className="readout">
          <dt>Answered</dt>
          <dd className="num">{state.done - state.errors}</dd>
        </div>
        <div className="readout">
          <dt>Errors</dt>
          <dd className="num">{state.errors}</dd>
        </div>
      </dl>
    </div>
  );
}
