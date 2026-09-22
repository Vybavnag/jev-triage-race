import { useEffect, useReducer } from "react";
import type { GateRow } from "./lib/gate";
import type { RaceEvent, Side, Totals } from "./types";

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
  correct: { urgent: number; team: number; frustration: number };
  scored: number;
  /** Per-ticket confidence and outcome, kept raw so the routing gate can be
   * recomputed at any threshold without re-running the race. */
  gateRows: GateRow[];
}

export interface FeedRow {
  side: Side;
  ticketId: string;
  latencyMs: number | null;
  kind: string | null;
  correct: { urgent: boolean | null; team: boolean | null; frustration: boolean | null } | null;
}

export interface RaceState {
  status: "idle" | "running" | "done" | "cancelled" | "error";
  total: number;
  elapsedMs: number | null;
  winner: Record<string, string> | null;
  gapped: boolean;
  sides: Record<Side, SideState>;
  feed: FeedRow[];
}

const FEED_LIMIT = 30;

const emptySide = (): SideState => ({
  model: "",
  done: 0,
  errors: 0,
  totals: null,
  lastLatency: null,
  costSoFar: 0,
  costKnown: true,
  correct: { urgent: 0, team: 0, frustration: 0 },
  scored: 0,
  gateRows: [],
});

export const initialState = (): RaceState => ({
  status: "idle",
  total: 0,
  elapsedMs: null,
  winner: null,
  gapped: false,
  sides: { jev: emptySide(), llm: emptySide() },
  feed: [],
});

type Action = { type: "reset"; total: number } | { type: "event"; event: RaceEvent };

function reducer(state: RaceState, action: Action): RaceState {
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
        sides: {
          jev: { ...state.sides.jev, model: data.sides?.jev ?? "" },
          llm: { ...state.sides.llm, model: data.sides?.llm ?? "" },
        },
      };

    case "result": {
      const side = state.sides[data.side as Side];
      const c = data.correct ?? {};
      return {
        ...state,
        sides: {
          ...state.sides,
          [data.side]: {
            ...side,
            done: side.done + 1,
            scored: side.scored + 1,
            lastLatency: data.latency_ms,
            costSoFar: side.costSoFar + (data.cost_usd ?? 0),
            costKnown: side.costKnown && data.cost_usd !== null,
            correct: {
              urgent: side.correct.urgent + (c.urgent ? 1 : 0),
              team: side.correct.team + (c.team ? 1 : 0),
              frustration: side.correct.frustration + (c.frustration ? 1 : 0),
            },
            gateRows: [
              ...side.gateRows,
              {
                confidence: data.verdict?.team_confidence ?? null,
                teamCorrect: Boolean(c.team),
              },
            ],
          },
        },
        feed: [
          {
            side: data.side,
            ticketId: data.ticket_id,
            latencyMs: data.latency_ms,
            kind: null,
            correct: data.correct,
          },
          ...state.feed,
        ].slice(0, FEED_LIMIT),
      };
    }

    case "error": {
      const side = state.sides[data.side as Side];
      return {
        ...state,
        sides: {
          ...state.sides,
          [data.side]: { ...side, done: side.done + 1, errors: side.errors + 1 },
        },
        feed: [
          { side: data.side, ticketId: data.ticket_id, latencyMs: null, kind: data.kind, correct: null },
          ...state.feed,
        ].slice(0, FEED_LIMIT),
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
      return { ...state, status: "done", elapsedMs: data.elapsed_ms, winner: data.winner };

    case "cancelled":
      return { ...state, status: "cancelled" };

    case "race_error":
      return { ...state, status: "error" };

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

    return () => {
      handlers.forEach(([kind, handler]) =>
        source.removeEventListener(kind, handler as EventListener),
      );
      source.close();
    };
  }, [raceId]);

  return { state, reset: (total: number) => dispatch({ type: "reset", total }) };
}
