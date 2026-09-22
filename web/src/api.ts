import { keyStore } from "./keys";
import type { AppConfig, Opponent, RaceSource } from "./types";

/** The race token, when the deployment sets one. Kept in memory only — never
 * localStorage, so it can't outlive the tab or be read by other scripts. */
let raceToken = "";

export function setRaceToken(token: string): void {
  raceToken = token.trim();
}

function authHeaders(): Record<string, string> {
  return raceToken ? { Authorization: `Bearer ${raceToken}` } : {};
}

/** The visitor's provider keys, sent only with requests that start work. */
function keyHeaders(): Record<string, string> {
  const keys = keyStore.get();
  if (!keys) return {};
  const headers: Record<string, string> = { "X-TypeSafe-Key": keys.typesafe };
  if (keys.anthropic) headers["X-Anthropic-Key"] = keys.anthropic;
  return headers;
}

/** In dev the page is served by Vite on :5173 and /api is proxied to the
 * backend on :8000. If only one of the two is running the proxy answers 502
 * or the fetch fails outright, so say which process is missing rather than
 * surfacing a bare status code. */
const BACKEND_DOWN =
  "The backend is not responding. Start it in a second terminal with " +
  "`uvicorn app.main:app --reload`, then reload this page.";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...authHeaders(), ...init?.headers },
    });
  } catch {
    throw new Error(BACKEND_DOWN);
  }
  if (res.status === 502 || res.status === 504) throw new Error(BACKEND_DOWN);
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b: { detail?: unknown; error?: string }) => describe(b.detail) ?? b.error)
      .catch(() => null);
    throw new Error(detail ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

/** A 422 carries a list of field errors; everything else a string. */
function describe(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { loc?: unknown[]; msg?: string };
    const field = (first.loc ?? []).filter((p) => p !== "body").join(".");
    const msg = (first.msg ?? "").replace(/^Value error, /, "");
    return field ? `${field}: ${msg}` : msg || null;
  }
  return null;
}

export const getConfig = () => request<AppConfig>("/api/config");

export const startRace = (opponent: Opponent, source: RaceSource) =>
  request<{ race_id: string; total: number }>("/api/race", {
    method: "POST",
    headers: keyHeaders(),
    body: JSON.stringify({ opponent, ...source }),
  });

export const cancelRace = async (raceId: string): Promise<void> => {
  await fetch(`/api/race/${raceId}`, { method: "DELETE", headers: authHeaders() });
};

export const startReview = (code: string, questions: string[], opponent: Opponent) =>
  request<{ review_id: string }>("/api/review", {
    method: "POST",
    headers: keyHeaders(),
    body: JSON.stringify({ code, questions, opponent }),
  });

export const cancelReview = async (reviewId: string): Promise<void> => {
  await fetch(`/api/review/${reviewId}`, { method: "DELETE", headers: authHeaders() });
};
