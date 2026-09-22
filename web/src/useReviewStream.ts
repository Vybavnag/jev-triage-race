import { useEffect, useReducer } from "react";
import type { ReviewEvent, ReviewSide, Side } from "./types";

/** The review_id is an unguessable capability, so EventSource needs no auth
 * header (which it could not send anyway). */

export interface ReviewSideState {
  model: string;
  /** null until this side's answer lands; an errored side is still a result. */
  result: ReviewSide | null;
}

export interface ReviewState {
  status: "idle" | "running" | "done" | "cancelled" | "error";
  questions: string[];
  sides: Record<Side, ReviewSideState>;
  elapsedMs: number | null;
  gapped: boolean;
  /** Why status is "error": the server's kind, or "stream_lost" when the
   * browser gave up reconnecting (a restart or an expired review). */
  failure: string | null;
}

export const initialState = (): ReviewState => ({
  status: "idle",
  questions: [],
  sides: { jev: { model: "", result: null }, llm: { model: "", result: null } },
  elapsedMs: null,
  gapped: false,
  failure: null,
});

export type Action = { type: "reset" } | { type: "event"; event: ReviewEvent };

export function reducer(state: ReviewState, action: Action): ReviewState {
  if (action.type === "reset") {
    return { ...initialState(), status: "running" };
  }
  const { event, data } = action.event as { event: string; data: any };

  switch (event) {
    case "start":
      return {
        ...state,
        status: "running",
        questions: data.questions ?? [],
        sides: {
          jev: { ...state.sides.jev, model: data.sides?.jev ?? "" },
          llm: { ...state.sides.llm, model: data.sides?.llm ?? "" },
        },
      };

    case "side_done": {
      const { side, ...result } = data as { side: Side } & ReviewSide;
      return {
        ...state,
        sides: { ...state.sides, [side]: { ...state.sides[side], result } },
      };
    }

    case "review_done":
      return { ...state, status: "done", elapsedMs: data.elapsed_ms };

    case "cancelled":
      return { ...state, status: "cancelled" };

    case "review_error":
      return { ...state, status: "error", failure: data.error ?? "internal_error" };

    case "gap":
      return { ...state, gapped: true };

    default:
      return state;
  }
}

export function useReviewStream(reviewId: string | null) {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);

  useEffect(() => {
    if (!reviewId) return;
    const source = new EventSource(`/api/review/${reviewId}/stream`);
    const kinds = ["start", "side_done", "review_done", "cancelled", "gap", "review_error"] as const;

    const handlers = kinds.map((kind) => {
      const handler = (e: MessageEvent) => {
        dispatch({ type: "event", event: { event: kind, data: JSON.parse(e.data) } as ReviewEvent });
        if (kind === "review_done" || kind === "cancelled" || kind === "review_error") source.close();
      };
      source.addEventListener(kind, handler as EventListener);
      return [kind, handler] as const;
    });
    // EventSource retries transient drops on its own. A permanent failure (a
    // 404 after a restart or an expired review) closes it for good, and no
    // terminal event will ever arrive: say so instead of staying "running".
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        dispatch({
          type: "event",
          event: { event: "review_error", data: { error: "stream_lost" } },
        });
      }
    };

    return () => {
      handlers.forEach(([kind, handler]) =>
        source.removeEventListener(kind, handler as EventListener),
      );
      source.close();
    };
  }, [reviewId]);

  return { state, reset: () => dispatch({ type: "reset" }) };
}
