/** One question per line. De-duplicated the way the server checks (case and
 * whitespace folded), first wording kept, capped at `max`. The server still
 * rejects duplicates, so this only spares the person a round trip. */
export function parseQuestions(raw: string, max: number): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    const q = line.trim();
    if (!q) continue;
    const key = q.toLowerCase().split(/\s+/).join(" ");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(q);
    if (out.length === max) break;
  }
  return out;
}
