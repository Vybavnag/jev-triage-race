import { errorLabel, ms } from "../lib/opponents";
import type { ReviewSide, Side } from "../types";
import { useElapsed } from "../useElapsed";

const money = (n: number | null) => (n === null ? "unknown" : `$${n.toFixed(6)}`);

/** One lane of the review race. Its track sweeps while the side is still
 * thinking, fills the moment its answer lands, and stripes on an error. */
export function ReviewChannel({
  side,
  label,
  model,
  result,
  running,
}: {
  side: Side;
  label: string;
  model: string;
  result: ReviewSide | null;
  running: boolean;
}) {
  const pending = running && result === null;
  const elapsed = useElapsed(pending);
  const error = result?.verdict.error ?? null;
  const answers = result?.verdict.answers ?? [];
  const yesCount = result && !error ? answers.filter((a) => a.yes).length : null;

  return (
    <div className={`card lane-card channel ${side}`}>
      <div className="channel-head">
        <div className="channel-name">
          <span className="lane-chip">{label}</span>
          {model && <span className="model">{model}</span>}
        </div>
        <div className="status">
          {pending ? "thinking…" : error ? <span className="err">{errorLabel(error)}</span> : result ? "answered" : "—"}
        </div>
      </div>
      <div className="track">
        <div className="track-row">
          {error ? (
            <div className="track-errors" style={{ width: "100%" }} />
          ) : pending ? (
            <div className="track-fill pending" />
          ) : (
            <div className="track-fill" style={{ width: result ? "100%" : "0%" }} />
          )}
        </div>
      </div>
      <dl className="readouts">
        <div className="readout readout--lead">
          <dt>{pending ? "Waiting for" : "Latency"}</dt>
          <dd className="num lead">
            {pending ? ms(elapsed) : result && !error ? ms(result.verdict.latency_ms) : "—"}
          </dd>
        </div>
        <div className="readout">
          <dt>Spend</dt>
          <dd className="num">{result ? money(result.cost_usd) : "—"}</dd>
        </div>
        <div className="readout">
          <dt>Answered yes</dt>
          <dd className="num">{yesCount === null ? "—" : `${yesCount} of ${answers.length}`}</dd>
        </div>
      </dl>
    </div>
  );
}
