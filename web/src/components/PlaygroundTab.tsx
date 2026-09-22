import { useState } from "react";
import { classify } from "../api";
import type { AppConfig, Opponent, PlaygroundResponse, SideResult } from "../types";

const SAMPLE =
  "Hi, I've been trying to connect my Stripe account for 3 days and it keeps " +
  "failing. I'm losing sales every hour this is down. Please help ASAP.";

function Panel({ side, result }: { side: "jev" | "llm"; result: SideResult }) {
  const v = result.verdict;
  const urgent = v.urgent_p;
  return (
    <div className={`result ${side}`}>
      <div className="channel-head">
        <div className="channel-name">
          {side === "jev" ? "Jev" : "LLM"}
          <span className="model">{result.provider}</span>
        </div>
        <div className="num">{Math.round(v.latency_ms).toLocaleString()} ms</div>
      </div>

      {v.error ? (
        <p className="err">Returned no classification: {v.error}</p>
      ) : (
        <dl className="answer">
          <dt>Urgent</dt>
          <dd>
            <span className="num">
              {urgent === null ? "—" : `${(urgent * 100).toFixed(1)}%`}
            </span>
            {urgent !== null && (
              <div className="dist">
                <span style={{ width: `${urgent * 100}%` }} />
              </div>
            )}
          </dd>

          <dt>Team</dt>
          <dd>
            <span className="answer-value">{v.team ?? "—"}</span>
            {v.team_confidence !== null && (
              <span className="num answer-note">
                {(v.team_confidence * 100).toFixed(0)}% confident
              </span>
            )}
            {v.team_distribution ? (
              <div className="dist-list">
                {v.team_distribution.map((o) => (
                  <div className="dist-row" key={o.option}>
                    <span className="dist-label">{o.option}</span>
                    <span className="dist">
                      <span style={{ width: `${o.p * 100}%` }} />
                    </span>
                    <span className="num dist-p">{o.p.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="no-dist">
                One team, asserted. There is no runner-up and no score to
                compare it against.
              </p>
            )}
          </dd>

          <dt>Frustration</dt>
          <dd>
            <span className="answer-value">{v.frustration ?? "—"}</span>
            {v.frustration_raw !== null && side === "jev" && (
              <span className="num answer-note">
                weighted {v.frustration_raw.toFixed(2)} on a 0–4 scale
              </span>
            )}
            {v.frustration_distribution ? (
              <div className="dist-list">
                {v.frustration_distribution.map((l) => (
                  <div className="dist-row" key={l.level}>
                    <span className="dist-label">{l.label}</span>
                    <span className="dist">
                      <span style={{ width: `${l.p * 100}%` }} />
                    </span>
                    <span className="num dist-p">{l.p.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="no-dist">
                A single level, with nothing to say how close the call was.
              </p>
            )}
          </dd>

          <dt>Cost</dt>
          <dd className="num">
            {result.cost_usd === null ? "unknown" : `$${result.cost_usd.toFixed(6)}`}
          </dd>
        </dl>
      )}

      <details>
        <summary>Request and response</summary>
        <pre>{JSON.stringify({ request: v.raw_request, response: v.raw_response }, null, 2)}</pre>
      </details>
    </div>
  );
}

export function PlaygroundTab({ config }: { config: AppConfig }) {
  const opponents: Opponent[] = [
    ...config.anthropic_models.map((m) => ({ kind: "anthropic" as const, model_id: m.id })),
    ...(config.openai_compat.enabled && config.openai_compat.model
      ? [{ kind: "openai_compat" as const, model_id: config.openai_compat.model }]
      : []),
  ];

  const [text, setText] = useState(SAMPLE);
  const [choice, setChoice] = useState(0);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [answer, setAnswer] = useState<PlaygroundResponse | null>(null);

  if (opponents.length === 0) {
    return <p className="notice">No opponent is configured. Set a provider key and restart.</p>;
  }

  async function onRun() {
    setBusy(true);
    setProblem(null);
    try {
      setAnswer(await classify(text, opponents[choice]));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Classification failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      {problem && <p className="notice">{problem}</p>}
      <label className="field">
        <span>Support message</span>
        <textarea
          value={text}
          maxLength={4000}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste a support message to classify"
        />
      </label>
      <div className="controls" style={{ marginTop: "1rem" }}>
        <label className="field">
          <span>Opponent</span>
          <select value={choice} onChange={(e) => setChoice(Number(e.target.value))}>
            {opponents.map((o, i) => (
              <option key={o.model_id} value={i}>
                {o.model_id}
              </option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={onRun} disabled={busy || text.trim().length === 0}>
          {busy ? "Classifying…" : "Classify both"}
        </button>
      </div>

      {answer && (
        <div className="side-by-side">
          <Panel side="jev" result={answer.jev} />
          <Panel side="llm" result={answer.llm} />
        </div>
      )}
    </section>
  );
}
