import { describe, expect, it } from "vitest";
import type { ReviewEvent, ReviewVerdict } from "./types";
import { initialState, reducer, type ReviewState } from "./useReviewStream";

const start: ReviewEvent = {
  event: "start",
  data: {
    review_id: "r",
    questions: ["Is there a secret?", "Bad call?"],
    sides: { jev: "jev-latest", llm: "claude-sonnet-5" },
  },
};

const verdict = (error: string | null = null): ReviewVerdict => ({
  answers: error ? [] : [{ id: "q1", p: 0.9, yes: true }, { id: "q2", p: 0.1, yes: false }],
  usage: { input_tokens: 1, output_tokens: 1 },
  latency_ms: 700,
  raw_request: null,
  raw_response: null,
  error,
});

const sideDone = (side: "jev" | "llm", error: string | null = null): ReviewEvent => ({
  event: "side_done",
  data: { side, provider: side === "jev" ? "jev-latest" : "claude-sonnet-5", verdict: verdict(error), cost_usd: error ? null : 0.001 },
});

function run(events: ReviewEvent[]): ReviewState {
  let state = reducer(initialState(), { type: "reset" });
  for (const event of events) state = reducer(state, { type: "event", event });
  return state;
}

describe("useReviewStream reducer", () => {
  it("starts with the questions and the two models, nothing answered yet", () => {
    const state = run([start]);
    expect(state.status).toBe("running");
    expect(state.questions).toEqual(["Is there a secret?", "Bad call?"]);
    expect(state.sides.jev.model).toBe("jev-latest");
    expect(state.sides.llm.model).toBe("claude-sonnet-5");
    expect(state.sides.jev.result).toBeNull();
    expect(state.sides.llm.result).toBeNull();
  });

  it("stores one side's answer while the other is still pending", () => {
    const state = run([start, sideDone("jev")]);
    expect(state.status).toBe("running");
    expect(state.sides.jev.result?.verdict.answers).toHaveLength(2);
    expect(state.sides.jev.result?.cost_usd).toBe(0.001);
    expect(state.sides.llm.result).toBeNull();
  });

  it("keeps an errored side as a result with its error kind", () => {
    const state = run([start, sideDone("llm", "timeout")]);
    expect(state.sides.llm.result?.verdict.error).toBe("timeout");
    expect(state.sides.llm.result?.cost_usd).toBeNull();
  });

  it("finishes with the wall clock", () => {
    const state = run([start, sideDone("jev"), sideDone("llm"), { event: "review_done", data: { elapsed_ms: 2900 } }]);
    expect(state.status).toBe("done");
    expect(state.elapsedMs).toBe(2900);
  });

  it("a cancelled review keeps whatever had arrived", () => {
    const state = run([start, sideDone("jev"), { event: "cancelled", data: {} }]);
    expect(state.status).toBe("cancelled");
    expect(state.sides.jev.result).not.toBeNull();
    expect(state.sides.llm.result).toBeNull();
  });

  it("a server error or a lost stream marks the run failed with the reason", () => {
    const failed = run([start, { event: "review_error", data: { error: "internal_error" } }]);
    expect(failed.status).toBe("error");
    expect(failed.failure).toBe("internal_error");
    const lost = run([start, sideDone("jev"), { event: "review_error", data: { error: "stream_lost" } }]);
    expect(lost.status).toBe("error");
    expect(lost.failure).toBe("stream_lost");
    expect(lost.sides.jev.result).not.toBeNull(); // what arrived is kept
    expect(reducer(lost, { type: "reset" }).failure).toBeNull();
  });

  it("reset clears the previous run", () => {
    const finished = run([start, sideDone("jev"), sideDone("llm"), { event: "review_done", data: { elapsed_ms: 1 } }]);
    const fresh = reducer(finished, { type: "reset" });
    expect(fresh.status).toBe("running");
    expect(fresh.questions).toEqual([]);
    expect(fresh.sides.jev.result).toBeNull();
    expect(fresh.elapsedMs).toBeNull();
  });
});
