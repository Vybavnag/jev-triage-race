import { describe, expect, it } from "vitest";
import { gateStats, type GateRow } from "./gate";

const row = (confidence: number | null, teamCorrect = true): GateRow => ({
  confidence,
  teamCorrect,
});

describe("gateStats", () => {
  it("treats the threshold as inclusive", () => {
    const rows = [row(0.79), row(0.8), row(0.81)];
    expect(gateStats(rows, 0.8).autoRouted).toBe(2);
    expect(gateStats(rows, 0.8).escalated).toBe(1);
  });

  it("returns nulls instead of dividing by zero", () => {
    const empty = gateStats([], 0.8);
    expect(empty.autoRoutedShare).toBeNull();
    expect(empty.autoRoutedAccuracy).toBeNull();
    expect(empty.scored).toBe(0);
  });

  it("ignores rows with no confidence, which is the LLM case", () => {
    const stats = gateStats([row(null), row(null, false), row(0.9)], 0.8);
    expect(stats.scored).toBe(1);
    expect(stats.autoRouted).toBe(1);
    // An all-null set must read as "nothing to gate", not "0% routed".
    expect(gateStats([row(null), row(null)], 0.8).autoRoutedShare).toBeNull();
  });

  it("counts wrong answers only inside the auto-routed slice", () => {
    // The low-confidence miss was escalated, so it must not be blamed on the gate.
    const rows = [row(0.95, false), row(0.95), row(0.4, false)];
    const stats = gateStats(rows, 0.8);
    expect(stats.autoRouted).toBe(2);
    expect(stats.autoRoutedWrong).toBe(1);
    expect(stats.autoRoutedAccuracy).toBeCloseTo(0.5);
  });

  it("routes everything at the floor and nothing above the ceiling", () => {
    const rows = [row(0.5), row(0.7), row(0.99)];
    expect(gateStats(rows, 0.5).autoRoutedShare).toBe(1);
    expect(gateStats(rows, 1).autoRouted).toBe(0);
    expect(gateStats(rows, 1).autoRoutedAccuracy).toBeNull();
  });

  it("raising the threshold never routes more", () => {
    const rows = [row(0.55), row(0.72), row(0.88), row(0.94)];
    const shares = [0.5, 0.6, 0.7, 0.8, 0.9, 1].map(
      (t) => gateStats(rows, t).autoRouted,
    );
    expect(shares).toEqual([...shares].sort((a, b) => b - a));
  });
});
