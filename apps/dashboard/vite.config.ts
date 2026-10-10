import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy the API so the browser talks to one origin (no CORS, SSE works).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:18000", changeOrigin: false } },
  },
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
});
