import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const backendTarget = process.env.VITE_BACKEND_TARGET ?? "http://127.0.0.1:8000";
const websocketTarget = backendTarget.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@gops/chart-engine": fileURLToPath(new URL("../chart-engine/src", import.meta.url))
    }
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts: ["stargops.com", "www.stargops.com"],
    proxy: {
      "/api": backendTarget,
      "/ws": {
        target: websocketTarget,
        ws: true
      }
    }
  }
});
