import { describe, expect, it } from "vitest";
import { parseQuestions } from "./questions";

describe("parseQuestions", () => {
  it("takes one question per line, trimmed", () => {
    expect(parseQuestions("  Is it safe? \nAny bug?", 10)).toEqual(["Is it safe?", "Any bug?"]);
  });

  it("drops blank lines", () => {
    expect(parseQuestions("A one?\n\n\nB two?\n", 10)).toEqual(["A one?", "B two?"]);
  });

  it("de-duplicates the way the server checks, keeping the first wording", () => {
    const raw = "Is it safe?\n is  IT safe? \nOther one?";
    expect(parseQuestions(raw, 10)).toEqual(["Is it safe?", "Other one?"]);
  });

  it("accepts Windows line endings", () => {
    expect(parseQuestions("A one?\r\nB two?", 10)).toEqual(["A one?", "B two?"]);
  });

  it("keeps the first N when over the cap", () => {
    const raw = Array.from({ length: 12 }, (_, i) => `Question ${i + 1}?`).join("\n");
    const out = parseQuestions(raw, 10);
    expect(out).toHaveLength(10);
    expect(out[9]).toBe("Question 10?");
  });

  it("returns nothing for empty input", () => {
    expect(parseQuestions("", 10)).toEqual([]);
  });
});
