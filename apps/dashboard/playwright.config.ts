import { defineConfig } from "@playwright/test";

// E2E against the running stack (`make up`). Base URL is the nginx-served dashboard.
export default defineConfig({
  testDir: "e2e",
  testMatch: /.*\.spec\.ts/,
  timeout: 60_000,
  use: { baseURL: process.env.DASHBOARD_URL ?? "http://127.0.0.1:13001", headless: true },
});
