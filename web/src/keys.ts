/** The visitor's own provider keys. They live in this tab: in memory, and
 * in sessionStorage so a reload does not ask again. Never localStorage, so
 * closing the tab forgets them. Each run sends them to this server in
 * headers; the server uses them for that run and keeps nothing. */

export interface ProviderKeys {
  typesafe: string;
  /** Empty when the visitor only uses an operator-configured opponent. */
  anthropic: string;
}

export interface KeyStore {
  get(): ProviderKeys | null;
  set(keys: ProviderKeys): void;
  clear(): void;
}

const STORAGE_KEY = "jev.keys";

function normalize(keys: Partial<ProviderKeys> | null | undefined): ProviderKeys | null {
  const typesafe = (keys?.typesafe ?? "").trim();
  const anthropic = (keys?.anthropic ?? "").trim();
  return typesafe ? { typesafe, anthropic } : null;
}

export function createKeyStore(storage: Storage | null): KeyStore {
  let memory: ProviderKeys | null = null;
  return {
    get() {
      if (memory) return memory;
      if (!storage) return null;
      try {
        const raw = storage.getItem(STORAGE_KEY);
        memory = raw ? normalize(JSON.parse(raw) as Partial<ProviderKeys>) : null;
      } catch {
        memory = null;
      }
      return memory;
    },
    set(keys) {
      memory = normalize(keys);
      if (!memory) return;
      try {
        storage?.setItem(STORAGE_KEY, JSON.stringify(memory));
      } catch {
        // Private mode or blocked site data: memory alone still works.
      }
    },
    clear() {
      memory = null;
      try {
        storage?.removeItem(STORAGE_KEY);
      } catch {
        // Nothing to forget where nothing could be written.
      }
    },
  };
}

function tabStorage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null; // some browsers throw on access when storage is blocked
  }
}

export const keyStore = createKeyStore(tabStorage());
