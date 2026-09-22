import { useState } from "react";
import { cancelRace, startRace } from "../api";
import { keyRejectedNotice, ms, opponentsFrom } from "../lib/opponents";
import { splitTickets } from "../lib/tickets";
import type { AppConfig, RaceSource } from "../types";
import { useRaceStream } from "../useRaceStream";
import { Channel } from "./Channel";
import { ResultsTable } from "./ResultsTable";

type Source = "bundled" | "own";

const winnerLabel = (who: string) =>
  who === "jev" ? "Jev" : who === "llm" ? "LLM" : who;

export function RaceTab({ config }: { config: AppConfig }) {
  const opponents = opponentsFrom(config);

  const [choice, setChoice] = useState(0);
  const [source, setSource] = useState<Source>("bundled");
  const [count, setCount] = useState(Math.min(10, config.race_max_items));
  const [pasted, setPasted] = useState("");
  /** The paragraphs sent with the current race, so id-only rows get their text. */
  const [sentOwn, setSentOwn] = useState<string[]>([]);
  const [raceId, setRaceId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const { state, reset } = useRaceStream(raceId);

  const running = state.status === "running";

  const allPasted = splitTickets(pasted, Number.MAX_SAFE_INTEGER);
  const own = allPasted.slice(0, config.max_own_tickets);
  const tooLong = own.filter((t) => t.length > config.max_ticket_chars).length;
  const canStart =
    !running && !starting && (source === "bundled" || (own.length > 0 && tooLong === 0));
  const firstError = (side: "jev" | "llm") =>
    Object.values(state.answers[side]).find((a) => a.error)?.error;
  const keyNotice = keyRejectedNotice({ jev: firstError("jev"), llm: firstError("llm") });

  async function onStart() {
    if (starting) return;
    setStarting(true);
    setProblem(null);
    const body: RaceSource = source === "own" ? { tickets: own } : { ticket_count: count };
    try {
      const { race_id, total } = await startRace(opponents[choice], body);
      setSentOwn(source === "own" ? own : []);
      reset(total);
      setRaceId(race_id);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Could not start the race");
    } finally {
      setStarting(false);
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
      {keyNotice && <p className="notice">{keyNotice}</p>}
      {state.gapped && (
        <p className="notice">
          Reconnected mid-race. Some events scrolled out of the replay buffer, so
          a few rows below may stay empty. Final totals are still exact.
        </p>
      )}

      <div className="card controls">
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
        <div className="field" role="group" aria-label="Which tickets to race">
          <span>Tickets</span>
          <div className="segmented">
            <button
              type="button"
              aria-pressed={source === "bundled"}
              disabled={running}
              onClick={() => setSource("bundled")}
            >
              Bundled, with expected answers
            </button>
            <button
              type="button"
              aria-pressed={source === "own"}
              disabled={running}
              onClick={() => setSource("own")}
            >
              Paste your own
            </button>
          </div>
        </div>
        <div className="run-row">
          {source === "bundled" && (
            <label className="field count">
              <span>How many (max {config.race_max_items})</span>
              <input
                type="number"
                inputMode="numeric"
                min={1}
                max={config.race_max_items}
                value={count}
                disabled={running}
                onChange={(e) =>
                  setCount(Math.max(1, Math.min(config.race_max_items, Number(e.target.value))))
                }
              />
            </label>
          )}
          <button className="primary" onClick={onStart} disabled={!canStart}>
            {running || starting ? "Racing…" : "Start race"}
          </button>
          {running && (
            <button className="secondary" onClick={onCancel}>
              Stop
            </button>
          )}
        </div>
      </div>

      {source === "own" && (
        <label className="field paste">
          <span>
            One ticket per paragraph. {own.length} of {config.max_own_tickets} tickets
            {allPasted.length > own.length && " (only the first ones are sent)"}
            {tooLong > 0 &&
              ` · ${tooLong} over ${config.max_ticket_chars.toLocaleString()} characters`}
          </span>
          <textarea
            value={pasted}
            disabled={running}
            onChange={(e) => setPasted(e.target.value)}
            placeholder={"Paste support messages here, separated by a blank line.\n\nLike this one, which starts a second ticket."}
          />
          <small className="hint">
            Your tickets have no expected answers, so the table shows both sides'
            answers and times and you compare them. They are sent to TypeSafe and
            the selected provider.
          </small>
        </label>
      )}

      <div className="lanes">
        <Channel side="jev" label="Jev" state={state.sides.jev} total={state.total} />
        <Channel side="llm" label="LLM" state={state.sides.llm} total={state.total} />
      </div>

      {state.status === "done" && state.winner && (
        <div className="verdict">
          <div>
            <div>Faster</div>
            <b className={`w-${state.winner.speed}`}>{winnerLabel(state.winner.speed)}</b>
          </div>
          <div>
            <div>Cheaper</div>
            <b className={`w-${state.winner.cost}`}>{winnerLabel(state.winner.cost)}</b>
          </div>
          <div>
            <div>Wall clock</div>
            <b className="num">{ms(state.elapsedMs)}</b>
          </div>
        </div>
      )}

      {state.status === "cancelled" && <p className="notice">Race stopped.</p>}
      {state.status === "error" && (
        <p className="notice">
          {state.failure === "stream_lost"
            ? "Lost the connection to this race, so its remaining results will not arrive. Start it again."
            : "The race failed on the server. Try again in a moment."}
        </p>
      )}

      {state.tickets.length > 0 && (
        <ResultsTable
          tickets={state.tickets}
          ownTexts={sentOwn}
          answers={state.answers}
          running={running}
        />
      )}
    </section>
  );
}
