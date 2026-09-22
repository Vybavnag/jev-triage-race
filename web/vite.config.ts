import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxies /api to the FastAPI backend so the SPA runs the same
// same-origin way it does inside the container.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  // assetsInlineLimit 0: nothing (fonts included) is ever inlined as a
  // data: URI, which the CSP (default-src 'self', no data:) would block.
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
  test: {
    // vitest stubs CSS imports to "" by default; the design guards read the
    // stylesheet as raw text, so let that one file through the pipeline.
    css: { include: [/styles\.css/] },
  },
});
