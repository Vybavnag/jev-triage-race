import type { AppConfig, Opponent } from "../types";

/** The LLM side is chosen from what the server allows, never typed in. */
export function opponentsFrom(config: AppConfig): Opponent[] {
  return [
    ...config.anthropic_models.map((m) => ({ kind: "anthropic" as const, model_id: m.id })),
    ...(config.openai_compat.enabled && config.openai_compat.model
      ? [{ kind: "openai_compat" as const, model_id: config.openai_compat.model }]
      : []),
  ];
}

export const ms = (n: number | null) => (n === null ? "—" : `${Math.round(n).toLocaleString()} ms`);

/** What a side's error kind means to the person watching. */
export function errorLabel(kind: string): string {
  switch (kind) {
    case "invalid_key":
      return "key rejected";
    case "rate_limited":
      return "rate limited";
    case "timeout":
      return "timed out";
    case "refusal":
      return "refused";
    case "malformed":
      return "unusable reply";
    case "upstream_error":
      return "provider error";
    default:
      return kind;
  }
}

/** A side that says the visitor's key was rejected: which provider to fix. */
export function keyRejectedNotice(kinds: { jev?: string | null; llm?: string | null }): string | null {
  if (kinds.jev === "invalid_key") return "TypeSafe rejected your key. Change it above and try again.";
  if (kinds.llm === "invalid_key") return "Anthropic rejected your key. Change it above and try again.";
  return null;
}
