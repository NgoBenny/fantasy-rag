import { defineConfig } from "@playwright/test";

// Paid, opt-in: one real chat question through the live API with a hard spending cap.
// Run: LIVE_LLM=1 npx playwright test -c playwright.paid.config.ts
// The API is its own process (never reused, one worker), so the cap covers the whole run.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /paid\.spec\.ts/,
  timeout: 120_000,
  workers: 1,
  use: { baseURL: "http://localhost:3020" },
  webServer: [
    {
      command: "uv run uvicorn fantasy_rag.api.main:app --port 8020 --workers 1",
      cwd: "../backend",
      url: "http://localhost:8020/api/health",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { LLM_BUDGET_USD: "0.10" },
    },
    {
      command: "npm run dev -- --port 3020",
      url: "http://localhost:3020",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { API_URL: "http://localhost:8020" },
    },
  ],
});
