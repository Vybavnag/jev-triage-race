import type { AppConfig, Opponent, PlaygroundResponse } from "./types";

/** The race token, when the deployment sets one. Kept in memory only — never
 * localStorage, so it can't outlive the tab or be read by other scripts. */
let raceToken = "";

export function setRaceToken(token: string): void {
  raceToken = token.trim();
}

function authHeaders(): Record<string, string> {
  return raceToken ? { Authorization: `Bearer ${raceToken}` } : {};
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
      .then((b: { detail?: string; error?: string }) => b.detail ?? b.error)
      .catch(() => null);
    throw new Error(detail ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const getConfig = () => request<AppConfig>("/api/config");

export const startRace = (opponent: Opponent, ticketCount: number) =>
  request<{ race_id: string; total: number }>("/api/race", {
    method: "POST",
    body: JSON.stringify({ opponent, ticket_count: ticketCount }),
  });

export const cancelRace = async (raceId: string): Promise<void> => {
  await fetch(`/api/race/${raceId}`, { method: "DELETE", headers: authHeaders() });
};

export const classify = (text: string, opponent: Opponent) =>
  request<PlaygroundResponse>("/api/playground", {
    method: "POST",
    body: JSON.stringify({ text, opponent }),
  });
