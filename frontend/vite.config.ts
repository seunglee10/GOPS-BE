import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = process.env.VITE_BACKEND_TARGET ?? "http://127.0.0.1:8000";
const agentTarget = process.env.VITE_AGENT_TARGET ?? "http://127.0.0.1:8010";
const websocketTarget = backendTarget.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api/chart-agent": agentTarget,
      "/api/charts": backendTarget,
      "/ws": {
        target: websocketTarget,
        ws: true
      }
    }
  }
});
