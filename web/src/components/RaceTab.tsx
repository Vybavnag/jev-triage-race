import { useState } from "react";
import { cancelRace, startRace } from "../api";
import { gateStats } from "../lib/gate";
import type { AppConfig, Opponent, Side } from "../types";
import { useRaceStream, type SideState } from "../useRaceStream";

const pct = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : "—");
const money = (n: number, known: boolean) => (known ? `$${n.toFixed(4)}` : "unknown");
const ms = (n: number | null) => (n === null ? "—" : `${Math.round(n).toLocaleString()} ms`);

function Channel({ side, label, state, total }: { side: Side; label: string; state: SideState; total: number }) {
  const donePct = total ? (state.done / total) * 100 : 0;
  const errPct = total ? (state.errors / total) * 100 : 0;
  return (
    <div className={`channel ${side}`}>
      <div className="channel-head">
        <div className="channel-name">
          {label}
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
        <div className="readout">
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
          <dt>Urgent</dt>
          <dd className="num">{pct(state.correct.urgent, state.scored)}</dd>
        </div>
        <div className="readout">
          <dt>Team</dt>
          <dd className="num">{pct(state.correct.team, state.scored)}</dd>
        </div>
        <div className="readout">
          <dt>Frustration</dt>
          <dd className="num">{pct(state.correct.frustration, state.scored)}</dd>
        </div>
        <div className="readout">
          <dt>Errors</dt>
          <dd className="num">{state.errors}</dd>
        </div>
      </dl>
    </div>
  );
}

/** The decision a probability buys you: let the confident ones route
 * themselves and send the rest to a person. Recomputed at render from the
 * per-ticket rows, so dragging the slider never re-runs the race. */
function RoutingGate({ jev, llm }: { jev: SideState; llm: SideState }) {
  const [threshold, setThreshold] = useState(0.8);
  const stats = gateStats(jev.gateRows, threshold);
  const llmStats = gateStats(llm.gateRows, threshold);

  if (stats.scored === 0) return null;

  return (
    <div className="gate">
      <h2>Route by confidence</h2>
      <p className="gate-intro">
        Jev returns a probability, so you can decide which tickets are certain
        enough to route on their own. Drag the bar to set where that line sits.
      </p>

      <label className="gate-slider">
        <span>Route automatically at or above</span>
        <input
          type="range"
          min={0.5}
          max={1}
          step={0.01}
          value={threshold}
          onChange={(e) => setThreshold(Number(e.target.value))}
        />
        <span className="num gate-threshold">{threshold.toFixed(2)}</span>
      </label>

      <div className="gate-grid">
        <div className="gate-stat">
          <div>Routed automatically</div>
          <b className="num">
            {stats.autoRoutedShare === null
              ? "—"
              : `${Math.round(stats.autoRoutedShare * 100)}%`}
          </b>
          <span className="num gate-sub">{stats.autoRouted} tickets</span>
        </div>
        <div className="gate-stat">
          <div>Sent to a person</div>
          <b className="num">
            {stats.autoRoutedShare === null
              ? "—"
              : `${Math.round((1 - stats.autoRoutedShare) * 100)}%`}
          </b>
          <span className="num gate-sub">{stats.escalated} tickets</span>
        </div>
        <div className="gate-stat">
          <div>Wrong among those routed</div>
          <b className={`num ${stats.autoRoutedWrong > 0 ? "gate-wrong" : ""}`}>
            {stats.autoRoutedWrong}
          </b>
          <span className="num gate-sub">
            {stats.autoRoutedAccuracy === null
              ? "none routed"
              : `${Math.round(stats.autoRoutedAccuracy * 100)}% correct`}
          </span>
        </div>
      </div>

      <p className="gate-note">
        {llmStats.scored === 0
          ? "The LLM returned no confidence score, so this slider has nothing to act on. Every one of its tickets is all-or-nothing: you either trust all of them or review all of them."
          : "Both sides reported confidence for this run."}
      </p>
    </div>
  );
}

export function RaceTab({ config }: { config: AppConfig }) {
  const opponents: Opponent[] = [
    ...config.anthropic_models.map((m) => ({ kind: "anthropic" as const, model_id: m.id })),
    ...(config.openai_compat.enabled && config.openai_compat.model
      ? [{ kind: "openai_compat" as const, model_id: config.openai_compat.model }]
      : []),
  ];

  const [choice, setChoice] = useState(0);
  const [count, setCount] = useState(Math.min(10, config.race_max_items));
  const [raceId, setRaceId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const { state, reset } = useRaceStream(raceId);

  const running = state.status === "running";

  async function onStart() {
    setProblem(null);
    try {
      const { race_id, total } = await startRace(opponents[choice], count);
      reset(total);
      setRaceId(race_id);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Could not start the race");
    }
  }

  async function onCancel() {
    if (raceId) await cancelRace(raceId);
  }

  if (opponents.length === 0) {
    return (
      <p className="notice">
        No opponent is configured. Set <code>ANTHROPIC_API_KEY</code>, or the{" "}
        <code>OPENAI_COMPAT_*</code> variables, then restart.
      </p>
    );
  }

  return (
    <section>
      {problem && <p className="notice">{problem}</p>}
      {state.gapped && (
        <p className="notice">
          Reconnected mid-race. Some events scrolled out of the replay buffer, so
          the feed below skips a stretch. Final totals are still exact.
        </p>
      )}

      <div className="controls">
        <label className="field">
          <span>Opponent</span>
          <select
            value={choice}
            disabled={running}
            onChange={(e) => setChoice(Number(e.target.value))}
          >
            {opponents.map((o, i) => (
              <option key={o.model_id} value={i}>
                {o.model_id}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Tickets (max {config.race_max_items})</span>
          <input
            type="number"
            min={1}
            max={config.race_max_items}
            value={count}
            disabled={running}
            onChange={(e) =>
              setCount(Math.max(1, Math.min(config.race_max_items, Number(e.target.value))))
            }
          />
        </label>
        <button className="primary" onClick={onStart} disabled={running}>
          {running ? "Racing…" : "Start race"}
        </button>
        {running && (
          <button className="secondary" onClick={onCancel}>
            Stop
          </button>
        )}
      </div>

      <Channel side="jev" label="Jev" state={state.sides.jev} total={state.total} />
      <Channel side="llm" label="LLM" state={state.sides.llm} total={state.total} />

      {state.status === "done" && state.winner && (
        <div className="verdict">
          {([
            ["Faster", state.winner.speed],
            ["Cheaper", state.winner.cost],
            ["More accurate", state.winner.accuracy],
          ] as const).map(([label, who]) => (
            <div key={label}>
              <div>{label}</div>
              <b className={who === "jev" || who === "llm" ? `w-${who}` : undefined}>
                {who === "jev" ? "Jev" : who === "llm" ? "LLM" : who}
              </b>
            </div>
          ))}
          <div>
            <div>Wall clock</div>
            <b className="num">{ms(state.elapsedMs)}</b>
          </div>
        </div>
      )}

      {state.status === "cancelled" && <p className="notice">Race stopped.</p>}

      {(state.status === "done" || state.status === "cancelled") && (
        <RoutingGate jev={state.sides.jev} llm={state.sides.llm} />
      )}

      <div className="feed">
        <h2>Live results</h2>
        <ol>
          {state.feed.map((row, i) => (
            <li key={`${row.side}-${row.ticketId}-${i}`}>
              <span className={`tag ${row.side}`}>{row.side}</span>
              <span>{row.ticketId}</span>
              {row.kind ? (
                <span className="err">{row.kind}</span>
              ) : (
                <>
                  <span className="num">{ms(row.latencyMs)}</span>
                  <span className={row.correct?.urgent ? "hit" : "miss"}>urgent</span>
                  <span className={row.correct?.team ? "hit" : "miss"}>team</span>
                  <span className={row.correct?.frustration ? "hit" : "miss"}>frustration</span>
                </>
              )}
            </li>
          ))}
          {state.feed.length === 0 && (
            <li>
              <span className="empty">
                Results appear here as each ticket is classified.
              </span>
            </li>
          )}
        </ol>
      </div>
    </section>
  );
}
