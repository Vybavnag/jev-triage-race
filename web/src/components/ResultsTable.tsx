import { errorLabel, ms } from "../lib/opponents";
import type { Expected, Side, TicketRow } from "../types";
import type { SideAnswer } from "../useRaceStream";

const yesNo = (v: boolean) => (v ? "yes" : "no");
const pct = (p: number) => `${Math.round(p * 100)}%`;

function ExpectedCell({ expected }: { expected: Expected | null }) {
  if (!expected) return <span className="answer-none">—</span>;
  return (
    <div className="answer-stack">
      <span>urgent {yesNo(expected.urgent)}</span>
      <span>{expected.team}</span>
      <span>frustration {expected.frustration}</span>
    </div>
  );
}

function AnswerCell({ answer, pending }: { answer: SideAnswer | undefined; pending: boolean }) {
  if (!answer) return <span className="answer-none">{pending ? "…" : "—"}</span>;
  if (answer.error) return <span className="err">{errorLabel(answer.error)}</span>;
  return (
    <div className="answer-stack">
      <span>
        urgent {answer.urgentP === null ? "—" : yesNo(answer.urgentP >= 0.5)}
        {answer.urgentP !== null && answer.urgentP !== 0 && answer.urgentP !== 1 && (
          <span className="num answer-p"> {pct(answer.urgentP)}</span>
        )}
      </span>
      <span>
        {answer.team ?? "—"}
        {answer.teamConfidence !== null && (
          <span className="num answer-p"> {pct(answer.teamConfidence)}</span>
        )}
      </span>
      <span>frustration {answer.frustration ?? "—"}</span>
      <span className="num answer-time">{ms(answer.latencyMs)}</span>
    </div>
  );
}

/** Visible only when the table collapses to cards on narrow screens; the
 * column headers, clipped but still in the tree, do the announcing. */
const CellLabel = ({ text }: { text: string }) => (
  <span className="cell-label" aria-hidden="true">
    {text}
  </span>
);

/** One row per ticket: what the label expects, then each side's answer with
 * its time. Nothing is marked right or wrong; the person reads across. The
 * ARIA roles repeat the native ones on purpose: they survive the CSS that
 * turns cells into blocks on phones. */
export function ResultsTable({
  tickets,
  ownTexts,
  answers,
  running,
}: {
  tickets: TicketRow[];
  /** The paragraphs the person pasted, in order, for rows the server sent as ids only. */
  ownTexts: string[];
  answers: Record<Side, Record<string, SideAnswer>>;
  running: boolean;
}) {
  return (
    <div className="results">
      <h2>Results</h2>
      <table className="compare-table results-table" role="table">
        <thead role="rowgroup" className="sr-only-narrow">
          <tr role="row">
            <th role="columnheader" scope="col">
              Ticket
            </th>
            <th role="columnheader" scope="col">
              Expected
            </th>
            <th role="columnheader" scope="col" className="jev">
              Jev
            </th>
            <th role="columnheader" scope="col" className="llm">
              LLM
            </th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {tickets.map((t, i) => {
            const text = t.text ?? ownTexts[i] ?? t.id;
            return (
              <tr key={t.id} role="row">
                <th role="rowheader" scope="row">
                  <span className="ticket-text" title={text}>
                    {text}
                  </span>
                </th>
                <td role="cell">
                  <CellLabel text="Expected" />
                  <ExpectedCell expected={t.expected} />
                </td>
                <td role="cell" className="jev">
                  <CellLabel text="Jev" />
                  <AnswerCell answer={answers.jev[t.id]} pending={running} />
                </td>
                <td role="cell" className="llm">
                  <CellLabel text="LLM" />
                  <AnswerCell answer={answers.llm[t.id]} pending={running} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="hint">
        Jev's percentages are how sure it is; the LLM asserts one value with no
        such number. Whether an answer is right is yours to judge.
      </p>
    </div>
  );
}
