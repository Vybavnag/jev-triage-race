import { useState } from "react";
import { cancelReview, startReview } from "../api";
import { keyRejectedNotice, ms, opponentsFrom } from "../lib/opponents";
import { parseQuestions } from "../lib/questions";
import type { AppConfig, ReviewSide } from "../types";
import { useReviewStream } from "../useReviewStream";
import { ReviewChannel } from "./ReviewChannel";
import { ReviewTable } from "./ReviewTable";

// A short snippet with a clear answer to each default question. It is
// deliberately flawed (a hardcoded password, a shell=True call, a swallowed
// exception, duplicated logic) and is only ever text in a textarea.
const SAMPLE_CODE = `import subprocess

DB_PASSWORD = "hunter2"  # TODO: move to the environment

def run(cmd):
    return subprocess.run(cmd, shell=True)

def read_config(path):
    try:
        return open(path).read()
    except Exception:
        pass

def total(items):
    t = 0
    for i in items:
        t = t + i["price"] * i["qty"]
    return t

def total_with_tax(items):
    t = 0
    for i in items:
        t = t + i["price"] * i["qty"]
    return t * 1.2
`;

/** Who was faster and cheaper, once both sides have answered without error. */
function verdictOf(jev: ReviewSide | null, llm: ReviewSide | null) {
  if (!jev || !llm || jev.verdict.error || llm.verdict.error) return null;
  const pick = (a: number | null, b: number | null) =>
    a === null || b === null ? "unknown" : a === b ? "tie" : a < b ? "Jev" : "LLM";
  return {
    faster: pick(jev.verdict.latency_ms, llm.verdict.latency_ms),
    cheaper: pick(jev.cost_usd, llm.cost_usd),
  };
}

export function ReviewTab({ config }: { config: AppConfig }) {
  const opponents = opponentsFrom(config);
  const { max_questions, max_code_chars, default_questions } = config.review;

  const [code, setCode] = useState(SAMPLE_CODE);
  const [rawQuestions, setRawQuestions] = useState(default_questions.join("\n"));
  const [choice, setChoice] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const { state, reset } = useReviewStream(reviewId);

  const running = state.status === "running";
  const questions = parseQuestions(rawQuestions, max_questions);
  // `starting` covers the gap between the click and the stream opening:
  // without it a double-click would start two paid reviews and orphan one.
  const canRun =
    !running &&
    !starting &&
    code.trim().length > 0 &&
    code.length <= max_code_chars &&
    questions.length > 0;
  const winner = state.status === "done" ? verdictOf(state.sides.jev.result, state.sides.llm.result) : null;
  const keyNotice = keyRejectedNotice({
    jev: state.sides.jev.result?.verdict.error,
    llm: state.sides.llm.result?.verdict.error,
  });

  if (opponents.length === 0) {
    return <p className="notice">No opponent is configured. Set a provider key and restart.</p>;
  }

  async function onRun() {
    if (starting) return;
    setStarting(true);
    setProblem(null);
    try {
      const { review_id } = await startReview(code, questions, opponents[choice]);
      reset();
      setReviewId(review_id);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Could not start the review");
    } finally {
      setStarting(false);
    }
  }

  async function onStop() {
    if (reviewId) await cancelReview(reviewId);
  }

  return (
    <section>
      {problem && <p className="notice">{problem}</p>}
      {keyNotice && <p className="notice">{keyNotice}</p>}
      {state.gapped && (
        <p className="notice">
          Reconnected mid-review. Some events scrolled out of the replay buffer.
        </p>
      )}

      <label className="field code card">
        <span>
          Code to review · {code.length.toLocaleString()} of {max_code_chars.toLocaleString()}{" "}
          characters
        </span>
        {/* wrap="off" keeps Safari from soft-wrapping code; the box scrolls
            sideways instead. No capitalisation or autocorrect on code. */}
        <textarea
          className="code-input"
          value={code}
          wrap="off"
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          disabled={running}
          onChange={(e) => setCode(e.target.value)}
          placeholder="Paste a function, a file, or a diff."
        />
      </label>

      <label className="field questions">
        <span>
          Questions, one per line · {questions.length} of {max_questions}
        </span>
        <textarea
          value={rawQuestions}
          disabled={running}
          onChange={(e) => setRawQuestions(e.target.value)}
          placeholder="Is there a hardcoded secret?"
        />
        <small className="hint">
          Both sides answer every question with yes or no; Jev also says how sure
          it is. Keep each one a plain question with a clear yes.{" "}
          <button
            type="button"
            className="link"
            disabled={running}
            onClick={() => setRawQuestions(default_questions.join("\n"))}
          >
            Reset to the defaults
          </button>
        </small>
      </label>

      <p className="hint">
        The code and the questions are sent to TypeSafe and the provider you pick
        below. Don't paste live credentials.
      </p>

      <div className="card controls">
        <label className="field">
          <span>Opponent</span>
          <select value={choice} disabled={running} onChange={(e) => setChoice(Number(e.target.value))}>
            {opponents.map((o, i) => (
              <option key={o.model_id} value={i}>
                {o.model_id}
              </option>
            ))}
          </select>
        </label>
        <div className="run-row">
          <button className="primary" onClick={onRun} disabled={!canRun}>
            {running || starting ? "Reviewing…" : "Review with both"}
          </button>
          {running && (
            <button className="secondary" onClick={onStop}>
              Stop
            </button>
          )}
        </div>
      </div>

      {state.status !== "idle" && (
        <div className="lanes">
          <ReviewChannel
            side="jev"
            label="Jev"
            model={state.sides.jev.model}
            result={state.sides.jev.result}
            running={running}
          />
          <ReviewChannel
            side="llm"
            label="LLM"
            model={state.sides.llm.model}
            result={state.sides.llm.result}
            running={running}
          />
        </div>
      )}

      {winner && (
        <div className="verdict">
          <div>
            <div>Faster</div>
            <b className={winner.faster === "Jev" ? "w-jev" : winner.faster === "LLM" ? "w-llm" : undefined}>
              {winner.faster}
            </b>
          </div>
          <div>
            <div>Cheaper</div>
            <b className={winner.cheaper === "Jev" ? "w-jev" : winner.cheaper === "LLM" ? "w-llm" : undefined}>
              {winner.cheaper}
            </b>
          </div>
          <div>
            <div>Wall clock</div>
            <b className="num">{ms(state.elapsedMs)}</b>
          </div>
        </div>
      )}

      {state.status === "cancelled" && <p className="notice">Review stopped.</p>}
      {state.status === "error" && (
        <p className="notice">
          {state.failure === "stream_lost"
            ? "Lost the connection to this review, so its remaining answers will not arrive. Start it again."
            : "The review failed on the server. Try again in a moment."}
        </p>
      )}

      {state.questions.length > 0 && (
        <ReviewTable
          questions={state.questions}
          jev={state.sides.jev.result}
          llm={state.sides.llm.result}
        />
      )}
    </section>
  );
}
