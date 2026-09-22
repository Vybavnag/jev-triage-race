/** Confidence routing: the decision you can only make when the model hands
 * back a probability instead of an assertion. Everything at or above the
 * threshold routes itself; the rest goes to a person. */

export interface GateRow {
  /** null when the provider returns no confidence — the LLM case. */
  confidence: number | null;
  teamCorrect: boolean;
}

export interface GateStats {
  /** Tickets carrying a confidence score at all. */
  scored: number;
  autoRouted: number;
  escalated: number;
  /** Of the auto-routed ones, how many went to the wrong team. */
  autoRoutedWrong: number;
  /** 0..1, or null when nothing was scored (avoids 0/0). */
  autoRoutedShare: number | null;
  /** Accuracy within the auto-routed slice, or null if none cleared the bar. */
  autoRoutedAccuracy: number | null;
}

export function gateStats(rows: GateRow[], threshold: number): GateStats {
  const scoredRows = rows.filter(
    (r): r is GateRow & { confidence: number } =>
      r.confidence !== null && Number.isFinite(r.confidence),
  );
  // At the threshold counts as clearing it: a 0.80 bar admits exactly 0.80.
  const above = scoredRows.filter((r) => r.confidence >= threshold);
  const wrong = above.filter((r) => !r.teamCorrect).length;
  const scored = scoredRows.length;

  return {
    scored,
    autoRouted: above.length,
    escalated: scored - above.length,
    autoRoutedWrong: wrong,
    autoRoutedShare: scored === 0 ? null : above.length / scored,
    autoRoutedAccuracy: above.length === 0 ? null : (above.length - wrong) / above.length,
  };
}
