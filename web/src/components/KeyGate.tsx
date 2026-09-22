import { useState, type FormEvent } from "react";
import { keyStore, type ProviderKeys } from "../keys";
import type { AppConfig } from "../types";

// Mirrors the server's shape check: 8 to 512 printable ASCII characters,
// no whitespace. Both SDKs refuse anything outside ASCII.
const KEY_SHAPE = /^[\x21-\x7e]{8,512}$/;

/** Asked once per tab, before anything runs: the visitor's own provider
 * keys. Nothing here is stored on the server; each run carries the keys in
 * its request and the server forgets them when the run ends. */
export function KeyGate({
  config,
  onReady,
}: {
  config: AppConfig;
  onReady: (keys: ProviderKeys) => void;
}) {
  const [typesafe, setTypesafe] = useState("");
  const [anthropic, setAnthropic] = useState("");
  const anthropicOptional = config.openai_compat.enabled;

  const looksLikeKey = (value: string) => KEY_SHAPE.test(value.trim());
  const ready = looksLikeKey(typesafe) && (anthropicOptional || looksLikeKey(anthropic));

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!ready) return;
    const keys = { typesafe: typesafe.trim(), anthropic: anthropic.trim() };
    keyStore.set(keys);
    onReady(keys);
  }

  return (
    <form className="key-gate card" onSubmit={handleSubmit}>
      <h2>Bring your own keys</h2>
      <p>
        Both sides run on your account, so nobody else's quota is spent. Your
        keys stay in this tab and are sent to this server only with each run,
        which uses them for that run and keeps nothing. Close the tab and they
        are gone.
      </p>

      <label className="field">
        <span className="label-row">
          <span>TypeSafe key</span>
          <a
            className="chip-link"
            href="https://console.typesafe.ai/keys"
            target="_blank"
            rel="noreferrer"
          >
            get one
          </a>
        </span>
        {/* A secret, not a login: no autofill, no capitalisation, and the
            password managers are asked to stay out. */}
        <input
          type="password"
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          value={typesafe}
          onChange={(e) => setTypesafe(e.target.value)}
          placeholder="Runs the Jev side"
        />
      </label>

      <label className="field">
        <span className="label-row">
          <span>Anthropic key{anthropicOptional ? " (optional)" : ""}</span>
          <a
            className="chip-link"
            href="https://console.anthropic.com/settings/keys"
            target="_blank"
            rel="noreferrer"
          >
            get one
          </a>
        </span>
        <input
          type="password"
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          value={anthropic}
          onChange={(e) => setAnthropic(e.target.value)}
          placeholder={
            anthropicOptional
              ? "Runs the Claude opponents; skip it to use the configured endpoint"
              : "Runs the Claude opponent"
          }
        />
      </label>

      <div className="key-gate-actions">
        <button className="primary" type="submit" disabled={!ready}>
          Use these keys
        </button>
        <small className="hint">
          Keys are checked by the providers on the first run. A rejected key shows
          up as "key rejected" on that side.
        </small>
      </div>
    </form>
  );
}
