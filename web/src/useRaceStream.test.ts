import { describe, expect, it } from "vitest";
import type { RaceEvent } from "./types";
import { initialState, reducer, type RaceState } from "./useRaceStream";

const start = (labeled: boolean): RaceEvent => ({
  event: "start",
  data: {
    race_id: "r",
    total: 2,
    sides: { jev: "jev-latest", llm: "claude-sonnet-5" },
    labeled,
    tickets: labeled
      ? [
          { id: "t1", text: "site is down", expected: { urgent: true, team: "technical", frustration: 4 } },
          { id: "t2", text: "invoice please", expected: { urgent: false, team: "billing", frustration: 1 } },
        ]
      : [
          { id: "p1", text: null, expected: null },
          { id: "p2", text: null, expected: null },
        ],
  },
});

const result = (side: "jev" | "llm", ticketId: string): RaceEvent => ({
  event: "result",
  data: {
    side,
    index: 0,
    ticket_id: ticketId,
    latency_ms: side === "jev" ? 300 : 2000,
    verdict: { urgent_p: 0.9, team: "technical", frustration: 4, team_confidence: 0.8 },
    usage: { input_tokens: 1, output_tokens: 1 },
    cost_usd: 0.001,
  },
});

function run(events: RaceEvent[], total = 2): RaceState {
  let state = reducer(initialState(), { type: "reset", total });
  for (const event of events) state = reducer(state, { type: "event", event });
  return state;
}

describe("useRaceStream reducer", () => {
  it("takes the tickets, their text and expected labels from the start event", () => {
    const state = run([start(true)]);
    expect(state.labeled).toBe(true);
    expect(state.tickets.map((t) => t.id)).toEqual(["t1", "t2"]);
    expect(state.tickets[0].text).toBe("site is down");
    expect(state.tickets[0].expected).toEqual({ urgent: true, team: "technical", frustration: 4 });
  });

  it("keeps ids only for pasted tickets", () => {
    const state = run([start(false)]);
    expect(state.labeled).toBe(false);
    expect(state.tickets[0]).toEqual({ id: "p1", text: null, expected: null });
  });

  it("files each side's answer under its ticket with its latency", () => {
    const state = run([start(true), result("jev", "t1"), result("llm", "t1")]);
    expect(state.answers.jev.t1).toEqual({
      urgentP: 0.9,
      team: "technical",
      teamConfidence: 0.8,
      frustration: 4,
      latencyMs: 300,
      error: null,
    });
    expect(state.answers.llm.t1?.latencyMs).toBe(2000);
    expect(state.answers.jev.t2).toBeUndefined();
    expect(state.sides.jev.done).toBe(1);
    expect(state.sides.jev.costSoFar).toBeCloseTo(0.001);
  });

  it("files an error as an answer with its kind so the row can show it", () => {
    const state = run([
      start(true),
      { event: "error", data: { side: "llm", index: 1, ticket_id: "t2", kind: "timeout" } },
    ]);
    expect(state.answers.llm.t2?.error).toBe("timeout");
    expect(state.answers.llm.t2?.team).toBeNull();
    expect(state.sides.llm.errors).toBe(1);
  });

  it("finishes with the measured winner and wall clock", () => {
    const done: RaceEvent = {
      event: "race_done",
      data: { elapsed_ms: 2100, totals: {} as never, winner: { speed: "jev", cost: "jev" } },
    };
    const state = run([start(true), done]);
    expect(state.status).toBe("done");
    expect(state.elapsedMs).toBe(2100);
    expect(state.winner).toEqual({ speed: "jev", cost: "jev" });
  });

  it("a server error or a lost stream marks the run failed with the reason", () => {
    const lost = run([start(true), result("jev", "t1"), { event: "race_error", data: { error: "stream_lost" } }]);
    expect(lost.status).toBe("error");
    expect(lost.failure).toBe("stream_lost");
    expect(lost.answers.jev.t1).toBeDefined(); // what arrived is kept
    expect(reducer(lost, { type: "reset", total: 1 }).failure).toBeNull();
  });

  it("reset clears tickets and answers", () => {
    const finished = run([start(true), result("jev", "t1")]);
    const fresh = reducer(finished, { type: "reset", total: 3 });
    expect(fresh.tickets).toEqual([]);
    expect(fresh.answers.jev).toEqual({});
    expect(fresh.total).toBe(3);
    expect(fresh.labeled).toBe(true);
  });
});
