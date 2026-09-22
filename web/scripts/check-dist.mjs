// Runs as the last step of `npm run build`. It fails the build when the
// bundle would need something the CSP (default-src 'self') forbids: a
// resource from another origin or a data: URI. It also insists the fonts
// were bundled, because the whole reason they are self-hosted is that CSP.
// Only resource loads are scanned; the JS bundle legitimately contains
// link targets such as the documentation URL.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const index = join(dist, "index.html");
if (!existsSync(index)) {
  console.error(`check-dist: ${index} is missing; run vite build first`);
  process.exit(1);
}

const problems = [];
const external = (value) => /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(value.trim());

const html = readFileSync(index, "utf8");
for (const m of html.matchAll(/<(?:link|script|img)\b[^>]*\b(?:href|src)=["']([^"']+)["']/gi)) {
  if (external(m[1])) problems.push(`index.html loads ${m[1]}`);
}

const assets = join(dist, "assets");
const files = existsSync(assets) ? readdirSync(assets) : [];
for (const name of files.filter((f) => f.endsWith(".css"))) {
  const css = readFileSync(join(assets, name), "utf8");
  for (const m of css.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)) {
    if (external(m[1])) problems.push(`${name} references ${m[1].slice(0, 60)}`);
  }
  for (const m of css.matchAll(/@import\s+(?:url\()?["']?([^"')\s;]+)/g)) {
    if (external(m[1])) problems.push(`${name} imports ${m[1]}`);
  }
}
if (!files.some((f) => f.endsWith(".woff2"))) {
  problems.push("no .woff2 in dist/assets: the fonts were not bundled");
}

if (problems.length > 0) {
  console.error(`check-dist: ${problems.length} problem(s)\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(`check-dist: ok (${files.length} assets, fonts bundled, no external resources)`);
