import { defineConfig } from "@playwright/test";

// Live, unpaid: the real API (dev Postgres with ingested data) on its own port with the
// Anthropic key blanked, so no test here can spend money; Next reaches it through
// e2e/proxy.mjs (8011) so tests can simulate an outage. Nothing is reused: stop your own
// `next dev` first (two dev servers in one folder conflict).
export default defineConfig({
  testDir: "./e2e",
  testIgnore: /(demo|paid)\.spec\.ts/, // own configs: playwright.demo / playwright.paid
  timeout: 60_000,
  workers: 1, // the outage test switches the shared proxy off; tests must not overlap
  use: { baseURL: "http://localhost:3010" },
  webServer: [
    {
      command: "uv run uvicorn fantasy_rag.api.main:app --port 8012",
      cwd: "../backend",
      url: "http://localhost:8012/api/health",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ANTHROPIC_API_KEY: "" },
    },
    {
      command: "node e2e/proxy.mjs",
      url: "http://localhost:8011/__up",
      reuseExistingServer: false,
      env: { UPSTREAM_PORT: "8012", PROXY_PORT: "8011" },
    },
    {
      command: "npm run dev -- --port 3010",
      url: "http://localhost:3010",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { API_URL: "http://localhost:8011" },
    },
  ],
});
