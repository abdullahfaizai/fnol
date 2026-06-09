import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// VITE_WS_BASE can override the backend WebSocket origin at build time
// (defaults to ws://<current-host>:8000 — see src/useAgent.js).
export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 5173 },
  preview: { host: true, port: 5173 },
});
