import { useEffect, useReducer } from "react";
import type { RaceEvent, Side, TicketRow, Totals, Winner } from "./types";

/** The race_id is an unguessable capability, so EventSource needs no auth
 * header (which it could not send anyway). */

export interface SideState {
  model: string;
  done: number;
  errors: number;
  totals: Totals | null;
  lastLatency: number | null;
  costSoFar: number;
  costKnown: boolean;
}

/** One side's answer for one ticket, as the results table shows it. An
 * errored call is still an answer row, carrying only its error kind. */
export interface SideAnswer {
  urgentP: number | null;
  team: string | null;
  teamConfidence: number | null;
  frustration: number | null;
  latencyMs: number | null;
  error: string | null;
}

export interface RaceState {
  status: "idle" | "running" | "done" | "cancelled" | "error";
  total: number;
  /** False for a run of pasted tickets: no expected labels to show. */
  labeled: boolean;
  tickets: TicketRow[];
  answers: Record<Side, Record<string, SideAnswer>>;
  elapsedMs: number | null;
  winner: Winner | null;
  gapped: boolean;
  /** Why status is "error": the server's kind, or "stream_lost" when the
   * browser gave up reconnecting (a restart or an expired race). */
  failure: string | null;
  sides: Record<Side, SideState>;
}

const emptySide = (): SideState => ({
  model: "",
  done: 0,
  errors: 0,
  totals: null,
  lastLatency: null,
  costSoFar: 0,
  costKnown: true,
});

export const initialState = (): RaceState => ({
  status: "idle",
  total: 0,
  labeled: true,
  tickets: [],
  answers: { jev: {}, llm: {} },
  elapsedMs: null,
  winner: null,
  gapped: false,
  failure: null,
  sides: { jev: emptySide(), llm: emptySide() },
});

export type Action = { type: "reset"; total: number } | { type: "event"; event: RaceEvent };

export function reducer(state: RaceState, action: Action): RaceState {
  if (action.type === "reset") {
    return { ...initialState(), status: "running", total: action.total };
  }
  const { event, data } = action.event as { event: string; data: any };

  switch (event) {
    case "start":
      return {
        ...state,
        status: "running",
        total: data.total,
        labeled: data.labeled ?? true,
        tickets: data.tickets ?? [],
        sides: {
          jev: { ...state.sides.jev, model: data.sides?.jev ?? "" },
          llm: { ...state.sides.llm, model: data.sides?.llm ?? "" },
        },
      };

    case "result": {
      const side = data.side as Side;
      const s = state.sides[side];
      const answer: SideAnswer = {
        urgentP: data.verdict?.urgent_p ?? null,
        team: data.verdict?.team ?? null,
        teamConfidence: data.verdict?.team_confidence ?? null,
        frustration: data.verdict?.frustration ?? null,
        latencyMs: data.latency_ms ?? null,
        error: null,
      };
      return {
        ...state,
        answers: { ...state.answers, [side]: { ...state.answers[side], [data.ticket_id]: answer } },
        sides: {
          ...state.sides,
          [side]: {
            ...s,
            done: s.done + 1,
            lastLatency: data.latency_ms,
            costSoFar: s.costSoFar + (data.cost_usd ?? 0),
            costKnown: s.costKnown && data.cost_usd !== null,
          },
        },
      };
    }

    case "error": {
      const side = data.side as Side;
      const s = state.sides[side];
      const answer: SideAnswer = {
        urgentP: null,
        team: null,
        teamConfidence: null,
        frustration: null,
        latencyMs: null,
        error: data.kind,
      };
      return {
        ...state,
        answers: { ...state.answers, [side]: { ...state.answers[side], [data.ticket_id]: answer } },
        sides: { ...state.sides, [side]: { ...s, done: s.done + 1, errors: s.errors + 1 } },
      };
    }

    case "done":
      return {
        ...state,
        sides: {
          ...state.sides,
          [data.side]: { ...state.sides[data.side as Side], totals: data.totals },
        },
      };

    case "race_done":
      return { ...state, status: "done", elapsedMs: data.elapsed_ms, winner: data.winner ?? null };

    case "cancelled":
      return { ...state, status: "cancelled" };

    case "race_error":
      return { ...state, status: "error", failure: data.error ?? "internal_error" };

    case "gap":
      return { ...state, gapped: true };

    default:
      return state;
  }
}

export function useRaceStream(raceId: string | null) {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);

  useEffect(() => {
    if (!raceId) return;
    const source = new EventSource(`/api/race/${raceId}/stream`);
    const kinds = [
      "start",
      "result",
      "error",
      "done",
      "race_done",
      "cancelled",
      "gap",
      "race_error",
    ] as const;

    const handlers = kinds.map((kind) => {
      const handler = (e: MessageEvent) => {
        dispatch({ type: "event", event: { event: kind, data: JSON.parse(e.data) } as RaceEvent });
        if (kind === "race_done" || kind === "cancelled" || kind === "race_error") source.close();
      };
      source.addEventListener(kind, handler as EventListener);
      return [kind, handler] as const;
    });
    // EventSource retries transient drops on its own. A permanent failure (a
    // 404 after a restart or an expired race) closes it for good, and no
    // terminal event will ever arrive: say so instead of staying "running".
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        dispatch({
          type: "event",
          event: { event: "race_error", data: { error: "stream_lost" } },
        });
      }
    };

    return () => {
      handlers.forEach(([kind, handler]) =>
        source.removeEventListener(kind, handler as EventListener),
      );
      source.close();
    };
  }, [raceId]);

  return { state, reset: (total: number) => dispatch({ type: "reset", total }) };
}
