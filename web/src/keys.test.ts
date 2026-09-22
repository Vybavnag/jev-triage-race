import { describe, expect, it } from "vitest";
import { createKeyStore } from "./keys";

/** Enough of the Storage interface for the store. */
function fakeStorage(): Storage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    get length() {
      return data.size;
    },
    key: (i: number) => [...data.keys()][i] ?? null,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
    clear: () => data.clear(),
  };
}

describe("key store", () => {
  it("round-trips both keys, trimmed", () => {
    const store = createKeyStore(fakeStorage());
    store.set({ typesafe: "  ts-key-123456  ", anthropic: " sk-ant-abc123 " });
    expect(store.get()).toEqual({ typesafe: "ts-key-123456", anthropic: "sk-ant-abc123" });
  });

  it("survives a reload through the storage it was given, and nothing else", () => {
    const storage = fakeStorage();
    createKeyStore(storage).set({ typesafe: "ts-key-123456", anthropic: "" });
    expect(createKeyStore(storage).get()).toEqual({ typesafe: "ts-key-123456", anthropic: "" });
    expect([...storage.data.keys()]).toEqual(["jev.keys"]);
  });

  it("forgets on clear", () => {
    const storage = fakeStorage();
    const store = createKeyStore(storage);
    store.set({ typesafe: "ts-key-123456", anthropic: "sk-ant-abc123" });
    store.clear();
    expect(store.get()).toBeNull();
    expect(storage.data.size).toBe(0);
  });

  it("treats malformed or empty storage as no keys", () => {
    const storage = fakeStorage();
    storage.setItem("jev.keys", "{not json");
    expect(createKeyStore(storage).get()).toBeNull();
    storage.setItem("jev.keys", JSON.stringify({ typesafe: "   ", anthropic: "x" }));
    expect(createKeyStore(storage).get()).toBeNull();
  });

  it("works in memory when no storage is available", () => {
    const store = createKeyStore(null);
    expect(store.get()).toBeNull();
    store.set({ typesafe: "ts-key-123456", anthropic: "" });
    expect(store.get()?.typesafe).toBe("ts-key-123456");
    store.clear();
    expect(store.get()).toBeNull();
  });

  it("keeps working when storage throws (private mode, blocked site data)", () => {
    const broken = fakeStorage();
    broken.setItem = () => {
      throw new Error("QuotaExceededError");
    };
    const store = createKeyStore(broken);
    store.set({ typesafe: "ts-key-123456", anthropic: "" });
    expect(store.get()?.typesafe).toBe("ts-key-123456");
  });
});
