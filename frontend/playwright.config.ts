import { defineConfig } from "@playwright/test";

// Runs against the real local stack: FastAPI on 8010 (dev Postgres with ingested data)
// and Next.js on 3010, which reaches the API through e2e/proxy.mjs (8011) so tests can
// simulate an outage. Servers are reused if already running.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: { baseURL: "http://localhost:3010" },
  webServer: [
    {
      command: "uv run uvicorn fantasy_rag.api.main:app --port 8010 --reload",
      cwd: "../backend",
      url: "http://localhost:8010/api/health",
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "node e2e/proxy.mjs",
      url: "http://localhost:8011/__up",
      reuseExistingServer: true,
    },
    {
      command: "npm run dev -- --port 3010",
      url: "http://localhost:3010",
      reuseExistingServer: true,
      timeout: 120_000,
      env: { API_URL: "http://localhost:8011" },
    },
  ],
});
