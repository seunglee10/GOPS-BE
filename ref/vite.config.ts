import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    watch: {
      ignored: ["**/references/**"]
    },
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true
      }
    }
  },
  optimizeDeps: {
    entries: ["index.html"],
    exclude: []
  },
  test: {
    environment: "node",
    include: ["src/tests/**/*.test.ts"],
    exclude: ["references/**", "node_modules/**", "dist/**"]
  }
});
