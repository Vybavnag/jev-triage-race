import { errorLabel, ms } from "../lib/opponents";
import type { ReviewSide, Side } from "../types";

const money = (n: number | null) => (n === null ? "unknown" : `$${n.toFixed(6)}`);

function YesNo({ yes }: { yes: boolean | null }) {
  if (yes === null) return <span className="answer-none">—</span>;
  return <span className={yes ? "answer-yes" : "answer-no"}>{yes ? "Yes" : "No"}</span>;
}

function SideHead({
  side,
  label,
  result,
}: {
  side: Side;
  label: string;
  result: ReviewSide | null;
}) {
  return (
    <div className={`review-head ${side}`}>
      <span className="lane-chip">{label}</span>
      {/* Model, status and cost repeat what the lane card above says, so
          they are visual only: a screen reader hears this column as "Jev"
          or "LLM", not a sentence, on every cell. */}
      <span className="review-head-meta" aria-hidden="true">
        {result && <span className="model">{result.provider}</span>}
        {result === null ? (
          <span className="review-meta">waiting…</span>
        ) : result.verdict.error ? (
          <span className="err">Returned no answer: {errorLabel(result.verdict.error)}</span>
        ) : (
          <span className="num review-meta">
            {ms(result.verdict.latency_ms)} · {money(result.cost_usd)}
          </span>
        )}
      </span>
    </div>
  );
}

function RawDetails({ label, result }: { label: string; result: ReviewSide }) {
  return (
    <details>
      <summary>{label} request and response</summary>
      <pre>
        {JSON.stringify(
          { request: result.verdict.raw_request, response: result.verdict.raw_response },
          null,
          2,
        )}
      </pre>
    </details>
  );
}

/** Visible only when the table collapses to cards on narrow screens. */
const CellLabel = ({ text }: { text: string }) => (
  <span className="cell-label" aria-hidden="true">
    {text}
  </span>
);

/** One row per question. Jev's column carries the probability as a bar with
 * the yes/no it implies; the LLM's column carries only the yes/no it asserted.
 * Rows fill in as each side's answer arrives; a side that has not answered
 * yet shows a placeholder, never a guess. The ARIA roles repeat the native
 * ones so the table stays a table once phones turn its cells into blocks. */
export function ReviewTable({
  questions,
  jev,
  llm,
}: {
  questions: string[];
  jev: ReviewSide | null;
  llm: ReviewSide | null;
}) {
  const answerOf = (side: ReviewSide | null, i: number) =>
    side === null || side.verdict.error ? null : side.verdict.answers[i] ?? null;
  return (
    <div className="review-result">
      <table className="compare-table review-table" role="table">
        <thead role="rowgroup" className="sr-only-narrow">
          <tr role="row">
            <th role="columnheader" scope="col">
              Question
            </th>
            <th role="columnheader" scope="col">
              <SideHead side="jev" label="Jev" result={jev} />
            </th>
            <th role="columnheader" scope="col">
              <SideHead side="llm" label="LLM" result={llm} />
            </th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {questions.map((q, i) => {
            const a = answerOf(jev, i);
            const b = answerOf(llm, i);
            const disagree = a?.yes != null && b?.yes != null && a.yes !== b.yes;
            return (
              <tr key={`q${i + 1}`} role="row" className={disagree ? "disagree" : undefined}>
                <th role="rowheader" scope="row">
                  {q}
                </th>
                <td role="cell" className="jev">
                  <CellLabel text="Jev" />
                  {a === null ? (
                    <span className="answer-none">{jev === null ? "…" : "—"}</span>
                  ) : (
                    <div className="review-answer">
                      <div className="dist">
                        <span style={{ width: `${(a.p ?? 0) * 100}%` }} />
                      </div>
                      <span className="num review-p">
                        {a.p === null ? "" : `${Math.round(a.p * 100)}%`}
                      </span>
                      <YesNo yes={a.yes} />
                    </div>
                  )}
                </td>
                <td role="cell" className="llm">
                  <CellLabel text="LLM" />
                  {b === null ? (
                    <span className="answer-none">{llm === null ? "…" : "—"}</span>
                  ) : (
                    <YesNo yes={b.yes} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="hint">
        A marked row is one the two sides answered differently. Jev's percentage
        is how sure it is of a yes; the LLM returns no such number.
      </p>
      {(jev || llm) && (
        <div className="review-raw">
          {jev && <RawDetails label="Jev" result={jev} />}
          {llm && <RawDetails label="LLM" result={llm} />}
        </div>
      )}
    </div>
  );
}
