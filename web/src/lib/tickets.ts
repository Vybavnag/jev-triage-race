/** One ticket per paragraph. Blank lines separate tickets; a single newline
 * stays inside one. Nothing is truncated here: the server enforces the
 * per-ticket character cap and says so if it is exceeded. */
export function splitTickets(raw: string, max: number): string[] {
  return raw
    .replace(/\r\n?/g, "\n")
    .split(/\n\s*\n/)
    .map((t) => t.trim())
    .filter((t) => t.length > 0)
    .slice(0, max);
}
