import { describe, expect, it } from "vitest";
import { splitTickets } from "./tickets";

describe("splitTickets", () => {
  it("splits on blank lines", () => {
    expect(splitTickets("a\n\nb", 25)).toEqual(["a", "b"]);
  });

  it("keeps a single newline inside one ticket", () => {
    expect(splitTickets("a\nb\n\nc", 25)).toEqual(["a\nb", "c"]);
  });

  it("trims each ticket and treats whitespace-only lines as separators", () => {
    expect(splitTickets(" a \n  \n\n b ", 25)).toEqual(["a", "b"]);
  });

  it("accepts Windows line endings", () => {
    expect(splitTickets("a\r\n\r\nb", 25)).toEqual(["a", "b"]);
  });

  it("returns nothing for empty or blank input", () => {
    expect(splitTickets("", 25)).toEqual([]);
    expect(splitTickets("\n \n", 25)).toEqual([]);
  });

  it("keeps the first N when over the cap", () => {
    const raw = Array.from({ length: 30 }, (_, i) => `t${i + 1}`).join("\n\n");
    const out = splitTickets(raw, 25);
    expect(out).toHaveLength(25);
    expect(out[0]).toBe("t1");
    expect(out[24]).toBe("t25");
  });

  it("never truncates a ticket: the server enforces the character cap", () => {
    const long = "x".repeat(5000);
    expect(splitTickets(long, 25)).toEqual([long]);
  });
});
