import { useEffect, useState } from "react";
import { getConfig, setRaceToken } from "./api";
import { HowItWorks } from "./components/HowItWorks";
import { KeyGate } from "./components/KeyGate";
import { RaceTab } from "./components/RaceTab";
import { ReviewTab } from "./components/ReviewTab";
import { keyStore, type ProviderKeys } from "./keys";
import type { AppConfig } from "./types";

type Tab = "race" | "review" | "how";

const TABS: { id: Tab; label: string }[] = [
  { id: "race", label: "Triage race" },
  { id: "review", label: "Code review" },
  { id: "how", label: "How it works" },
];

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("race");
  const [token, setToken] = useState("");
  const [keys, setKeys] = useState<ProviderKeys | null>(() => keyStore.get());

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
        <p className="loading">Loading…</p>
      </div>
    );
  }

  function forgetKeys() {
    keyStore.clear();
    setKeys(null);
  }

  return (
    <div className="app">
      <header className="hero">
        {/* The title doubles as the colour key: Jev's blue is also the page's
            accent, the LLM's violet appears only in its own lane. */}
        <h1>
          <span className="t-jev">Jev</span> <span className="t-vs">vs</span>{" "}
          <span className="t-llm">LLM</span>
        </h1>
        <p className="lede">
          A classifier and a language model answer the same questions about the
          same text. Race them on support tickets, yours or ours, or hand them
          both a block of code to review.
        </p>
      </header>

      {!keys ? (
        <KeyGate config={config} onReady={setKeys} />
      ) : (
        <>
          <div className="key-status">
            Using your keys for this tab{keys.anthropic ? "" : " (TypeSafe only)"}.{" "}
            <button type="button" className="link" onClick={forgetKeys}>
              Change or forget them
            </button>
          </div>

          {config.token_required && (
            <div className="card controls token-gate">
              <label className="field">
                <span>Race token</span>
                <input
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  data-1p-ignore
                  data-lpignore="true"
                  data-bwignore
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

          <nav className="tabs" aria-label="Sections">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                aria-current={tab === t.id}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <main>
            {tab === "race" && <RaceTab config={config} />}
            {tab === "review" && <ReviewTab config={config} />}
            {tab === "how" && <HowItWorks config={config} />}
          </main>
        </>
      )}

      <footer>
        Jev model {config.jev.model} · {config.dataset_size} labeled tickets bundled ·{" "}
        <a href="https://docs.typesafe.ai/introduction">Jev documentation</a>
      </footer>
    </div>
  );
}
