import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// Self-hosted faces, latin subsets only, so every font is served from this
// origin and stays inside the CSP. Poppins carries display text, Manrope the
// body, JetBrains Mono the measured numbers.
import "@fontsource/poppins/latin-600.css";
import "@fontsource/poppins/latin-800.css";
import "@fontsource/manrope/latin-400.css";
import "@fontsource/manrope/latin-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
