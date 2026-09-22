/**
 * Source-level guards for the design rules the page keeps restating: solid
 * fills, one neutral shadow, 16px form controls (iOS zooms on anything
 * smaller), no fixed chrome that fights the mobile keyboard, labelled
 * secret inputs, and keys that never touch localStorage. They read the
 * source as text, so they need no DOM.
 */
import { describe, expect, it } from "vitest";
import indexHtml from "../index.html?raw";
import appTsx from "./App.tsx?raw";
import keyGateTsx from "./components/KeyGate.tsx?raw";
import reviewTableTsx from "./components/ReviewTable.tsx?raw";
import styles from "./styles.css?raw";

const sources = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const componentSources = Object.entries(sources).filter(([path]) => !path.includes(".test."));

const css = styles.replace(/\/\*[\s\S]*?\*\//g, "");

/** Innermost `selector { body }` blocks, which also covers rules nested in
 * media queries because those have no braces of their own inside. */
function rules(text: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) out.push({ selector: m[1].trim(), body: m[2] });
  return out;
}

function declarations(body: string, property: string): string[] {
  const re = new RegExp(`(?:^|;|\\s)${property}\\s*:\\s*([^;]+)`, "g");
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(body))) out.push(m[1].trim());
  return out;
}

/** Split on commas that are not inside parentheses. */
function layers(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of value) {
    if (ch === "(") depth += 1;
    if (ch === ")") depth -= 1;
    if (ch === "," && depth === 0) {
      out.push(current.trim());
      current = "";
    } else current += ch;
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

/** True when a font-size value is provably at least 16px. */
function atLeast16px(value: string): boolean {
  if (value === "inherit" || value === "1rem" || value === "100%") return true;
  const m = /^(\d*\.?\d+)(rem|em|px|%)$/.exec(value);
  if (!m) return false;
  const n = Number(m[1]);
  return m[2] === "px" ? n >= 16 : m[2] === "%" ? n >= 100 : n >= 1;
}

const controlRules = rules(css).filter((r) => /\b(input|textarea|select)\b|\.code-input/.test(r.selector));

describe("page shell", () => {
  it("reads the real sources (guards against vacuous passes)", () => {
    expect(css).toContain(":root");
    expect(indexHtml).toContain("<!doctype html>");
    expect(componentSources.length).toBeGreaterThan(10);
  });

  it("index.html declares theme-color #f6f3ec and color-scheme light", () => {
    expect(indexHtml).toMatch(/<meta name="theme-color" content="#f6f3ec" \/>/);
    expect(indexHtml).toMatch(/<meta name="color-scheme" content="light" \/>/);
  });

  it("root font size is 16px and form controls are at least 1rem", () => {
    const html = rules(css).filter((r) => r.selector === "html");
    expect(html.flatMap((r) => declarations(r.body, "font-size"))).toEqual(["16px"]);
    const explicit = controlRules.filter(
      (r) => /\binput\b/.test(r.selector) && /\bselect\b/.test(r.selector) && /\btextarea\b/.test(r.selector),
    );
    expect(explicit.flatMap((r) => declarations(r.body, "font-size"))).toContain("1rem");
    for (const r of controlRules) {
      for (const value of declarations(r.body, "font-size")) {
        expect(atLeast16px(value), `${r.selector} sets font-size ${value}`).toBe(true);
      }
    }
  });
});

describe("solid fills", () => {
  it("gradient( appears only inside .track-errors", () => {
    for (const r of rules(css)) {
      if (r.body.includes("gradient(")) expect(r.selector).toBe(".track-errors");
    }
    expect(css).not.toMatch(/background-image/);
  });

  it("every box-shadow layer is inset or the one neutral shadow", () => {
    for (const r of rules(css)) {
      for (const value of declarations(r.body, "box-shadow")) {
        for (const layer of layers(value)) {
          expect(layer === "var(--shadow)" || layer.startsWith("inset "), `${r.selector}: ${layer}`).toBe(true);
        }
      }
    }
  });

  it("--shadow is neutral, low alpha, small blur", () => {
    const token = /--shadow\s*:\s*([^;]+);/.exec(css);
    expect(token, "no --shadow token").not.toBeNull();
    for (const layer of layers(token![1])) {
      const parts = layer.split(/\s+/);
      const colour = /^rgba\(21,23,27,(0?\.\d+)\)$/.exec(parts.at(-1) ?? "");
      expect(colour, `${layer} is not a neutral rgba`).not.toBeNull();
      expect(Number(colour![1])).toBeLessThanOrEqual(0.08);
      const blur = Number.parseFloat(parts[2] ?? "0");
      expect(blur).toBeLessThanOrEqual(24);
    }
  });
});

describe("mobile chrome", () => {
  it("nothing is fixed or sticky and nothing uses 100vh", () => {
    expect(css).not.toMatch(/position\s*:\s*(fixed|sticky)/);
    expect(css).not.toMatch(/100vh/);
    for (const [path, text] of componentSources) {
      expect(text, path).not.toMatch(/position\s*:\s*["']?(fixed|sticky)/);
      expect(text, path).not.toMatch(/100vh/);
    }
  });

  it("collapsed-table labels come from markup, not ::before content", () => {
    expect(css).not.toMatch(/content\s*:\s*["'](Jev|LLM)["']/);
  });
});

const SECRET_ATTRIBUTES = [
  'type="password"',
  'autoComplete="off"',
  'autoCapitalize="none"',
  'autoCorrect="off"',
  "spellCheck={false}",
  "data-1p-ignore",
  'data-lpignore="true"',
  "data-bwignore",
];

const passwordInputs = (source: string) =>
  (source.match(/<input\b[\s\S]*?\/>/g) ?? []).filter((tag) => tag.includes('type="password"'));

describe("collapsed tables", () => {
  it("review column headers announce only the lane name", () => {
    // The header row is clipped on phones but stays in the accessibility
    // tree, so its status/model/cost must be visual-only or every cell is
    // announced with a paragraph-long column name.
    const head = /<span className="review-head-meta" aria-hidden="true">[\s\S]*?<\/span>\s*<\/div>/.exec(reviewTableTsx);
    expect(head, "meta wrapper missing").not.toBeNull();
    expect(head![0]).toContain("review-meta");
    expect(head![0]).toContain("provider");
  });
});

describe("secret inputs", () => {
  it("both key inputs carry every autofill and password-manager opt-out", () => {
    const inputs = passwordInputs(keyGateTsx);
    expect(inputs).toHaveLength(2);
    for (const tag of inputs) for (const attr of SECRET_ATTRIBUTES) expect(tag, attr).toContain(attr);
  });

  it("the race-token input carries the same attributes", () => {
    const inputs = passwordInputs(appTsx);
    expect(inputs).toHaveLength(1);
    for (const attr of SECRET_ATTRIBUTES) expect(inputs[0], attr).toContain(attr);
  });

  it("no source file touches localStorage", () => {
    for (const [path, text] of componentSources) {
      expect(text, path).not.toMatch(/\blocalStorage\s*[.[(]/);
    }
  });
});
