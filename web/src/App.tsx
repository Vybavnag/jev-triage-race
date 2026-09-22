import { useEffect, useState } from "react";
import { getConfig, setRaceToken } from "./api";
import { HowItWorks } from "./components/HowItWorks";
import { PlaygroundTab } from "./components/PlaygroundTab";
import { RaceTab } from "./components/RaceTab";
import type { AppConfig } from "./types";

type Tab = "race" | "playground" | "how";

const TABS: { id: Tab; label: string }[] = [
  { id: "race", label: "Race" },
  { id: "playground", label: "Playground" },
  { id: "how", label: "How it works" },
];

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("race");
  const [token, setToken] = useState("");

  useEffect(() => {
    getConfig().then(setConfig).catch((e: Error) => setFailure(e.message));
  }, []);

  if (failure) {
    return (
      <div className="app">
        <p className="notice">Could not load configuration: {failure}</p>
      </div>
    );
  }
  if (!config) {
    return (
      <div className="app">
        <p>Loading…</p>
      </div>
    );
  }

  return (
    <div className="app">
      <header>
        <h1>Jev vs LLM: triage race</h1>
        <p>
          The same support tickets, the same three questions, graded against the
          same labels. One side is a classifier, the other is a language model.
        </p>
      </header>

      {config.token_required && (
        <div className="controls" style={{ marginTop: "1.5rem" }}>
          <label className="field">
            <span>Race token</span>
            <input
              type="password"
              value={token}
              placeholder="Required by this deployment"
              onChange={(e) => {
                setToken(e.target.value);
                setRaceToken(e.target.value);
              }}
            />
          </label>
        </div>
      )}

      <nav>
        {TABS.map((t) => (
          <button key={t.id} aria-current={tab === t.id} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "race" && <RaceTab config={config} />}
      {tab === "playground" && <PlaygroundTab config={config} />}
      {tab === "how" && <HowItWorks config={config} />}

      <footer>
        Jev model {config.jev.model} · {config.dataset_size} labeled tickets bundled ·{" "}
        <a href="https://docs.typesafe.ai/introduction">Jev documentation</a>
      </footer>
    </div>
  );
}
