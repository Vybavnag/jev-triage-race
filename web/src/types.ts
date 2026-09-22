export type Side = "jev" | "llm";

export interface AppConfig {
  jev: { model: string; price_in: number; price_out: number };
  anthropic_models: { id: string; price_in: number; price_out: number }[];
  openai_compat: { enabled: boolean; model: string | null; price_source: string };
  dataset_size: number;
  race_max_items: number;
  token_required: boolean;
}

export interface Opponent {
  kind: "anthropic" | "openai_compat";
  model_id: string;
}

export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface TeamOption {
  option: string;
  p: number;
}

export interface FrustrationLevel {
  level: number;
  label: string;
  p: number;
}

export interface Verdict {
  urgent_p: number | null;
  team: string | null;
  team_confidence: number | null;
  /** Null for the LLM: it asserts one value per field, with no distribution. */
  team_distribution: TeamOption[] | null;
  frustration_distribution: FrustrationLevel[] | null;
  frustration_raw: number | null;
  frustration: number | null;
  usage: Usage;
  latency_ms: number;
  raw_request: Record<string, unknown> | null;
  raw_response: Record<string, unknown> | null;
  error: string | null;
}

export interface SideResult {
  provider: string;
  verdict: Verdict;
  cost_usd: number | null;
}

export interface PlaygroundResponse {
  jev: SideResult;
  llm: SideResult;
}

export interface Accuracy {
  urgent: number | null;
  team: number | null;
  frustration: number | null;
}

export interface Totals {
  attempted: number;
  scored: number;
  errors: number;
  error_kinds: Record<string, number>;
  accuracy: Accuracy;
  avg_latency_ms: number | null;
  total_cost_usd: number | null;
}

export type RaceEvent =
  | { event: "start"; data: { race_id: string; total: number; sides: Record<Side, string> } }
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
        correct: { urgent: boolean | null; team: boolean | null; frustration: boolean | null };
        usage: Usage;
        cost_usd: number | null;
      };
    }
  | { event: "error"; data: { side: Side; index: number; ticket_id: string; kind: string } }
  | { event: "done"; data: { side: Side; totals: Totals } }
  | {
      event: "race_done";
      data: {
        elapsed_ms: number;
        totals: Record<Side, Totals>;
        winner: Record<string, string> | null;
      };
    }
  | { event: "cancelled"; data: { totals: Record<Side, Totals> } }
  | { event: "gap"; data: { from: number; to: number } }
  | { event: "race_error"; data: { error: string } };
