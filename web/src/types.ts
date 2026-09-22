export type Side = "jev" | "llm";

export interface ReviewConfig {
  default_questions: string[];
  max_questions: number;
  max_question_chars: number;
  max_code_chars: number;
}

export interface AppConfig {
  jev: { model: string; price_in: number; price_out: number };
  anthropic_models: { id: string; price_in: number; price_out: number }[];
  openai_compat: { enabled: boolean; model: string | null; price_source: string };
  dataset_size: number;
  /** Bundled tickets per race, clamped to the dataset. */
  race_max_items: number;
  /** Pasted tickets per race: the operator's cap alone. */
  max_own_tickets: number;
  max_ticket_chars: number;
  review: ReviewConfig;
  token_required: boolean;
}

export interface Opponent {
  kind: "anthropic" | "openai_compat";
  model_id: string;
}

/** What starts a race: a count of bundled, labeled tickets, or your own. */
export type RaceSource = { ticket_count: number } | { tickets: string[] };

export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
}

// ── Code review ──────────────────────────────────────────────────────────────

export interface ReviewAnswer {
  id: string;
  /** Jev's probability of "yes"; null for the LLM, which only asserts. */
  p: number | null;
  yes: boolean | null;
}

export interface ReviewVerdict {
  answers: ReviewAnswer[];
  usage: Usage;
  latency_ms: number;
  raw_request: Record<string, unknown> | null;
  raw_response: Record<string, unknown> | null;
  error: string | null;
}

export interface ReviewSide {
  provider: string;
  verdict: ReviewVerdict;
  cost_usd: number | null;
}

/** A review streams like a race: each side's answer arrives on its own. */
export type ReviewEvent =
  | {
      event: "start";
      data: { review_id: string; questions: string[]; sides: Record<Side, string> };
    }
  | { event: "side_done"; data: { side: Side } & ReviewSide }
  | { event: "review_done"; data: { elapsed_ms: number } }
  | { event: "cancelled"; data: Record<string, never> }
  | { event: "gap"; data: { from: number; to: number } }
  | { event: "review_error"; data: { error: string } };

// ── Race stream ──────────────────────────────────────────────────────────────

export interface Totals {
  attempted: number;
  scored: number;
  errors: number;
  error_kinds: Record<string, number>;
  avg_latency_ms: number | null;
  total_cost_usd: number | null;
}

/** What a bundled ticket's label says. The person compares; nothing grades. */
export interface Expected {
  urgent: boolean;
  team: string;
  frustration: number;
}

/** A ticket as the start event describes it. Pasted tickets are ids only:
 * the client already has their text, and frames never echo it. */
export interface TicketRow {
  id: string;
  text: string | null;
  expected: Expected | null;
}

/** Who was faster and cheaper: "jev", "llm", "tie", or "none". */
export interface Winner {
  speed: string;
  cost: string;
}

export type RaceEvent =
  | {
      event: "start";
      data: {
        race_id: string;
        total: number;
        sides: Record<Side, string>;
        labeled: boolean;
        tickets: TicketRow[];
      };
    }
  | {
      event: "result";
      data: {
        side: Side;
        index: number;
        ticket_id: string;
        latency_ms: number;
        verdict: {
          urgent_p: number | null;
          team: string | null;
          frustration: number | null;
          team_confidence: number | null;
        };
        usage: Usage;
        cost_usd: number | null;
      };
    }
  | { event: "error"; data: { side: Side; index: number; ticket_id: string; kind: string } }
  | { event: "done"; data: { side: Side; totals: Totals } }
  | {
      event: "race_done";
      data: { elapsed_ms: number; totals: Record<Side, Totals>; winner: Winner | null };
    }
  | { event: "cancelled"; data: { totals: Record<Side, Totals> } }
  | { event: "gap"; data: { from: number; to: number } }
  | { event: "race_error"; data: { error: string } };
