import { useEffect, useState } from "react";

/** Milliseconds since `active` last became true, ticking while it stays
 * true. Gives a pending side a live clock until its real latency arrives. */
export function useElapsed(active: boolean): number {
  const [ms, setMs] = useState(0);

  useEffect(() => {
    if (!active) return;
    const started = performance.now();
    setMs(0);
    const id = window.setInterval(() => setMs(performance.now() - started), 100);
    return () => window.clearInterval(id);
  }, [active]);

  return ms;
}
