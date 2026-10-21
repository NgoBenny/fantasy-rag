import { defineConfig } from "@playwright/test";

// Demo mode (CI): the API serves labelled sample data with no database, no API key and
// no .env (FANTASY_RAG_ENV_FILE points at a file that doesn't exist). Own ports, so it
// never reuses a live server. Next runs as a production build.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /demo\.spec\.ts/,
  timeout: 60_000,
  workers: 1, // the outage test switches the shared proxy off; tests must not overlap
  use: { baseURL: "http://localhost:3030" },
  webServer: [
    {
      command: "uv run uvicorn fantasy_rag.api.main:app --port 8030",
      cwd: "../backend",
      url: "http://localhost:8030/api/health",
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        DEMO_MODE: "true",
        FANTASY_RAG_ENV_FILE: "no-such-file.env",
        DATABASE_URL: "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        ANTHROPIC_API_KEY: "",
      },
    },
    {
      command: "node e2e/proxy.mjs",
      url: "http://localhost:8031/__up",
      reuseExistingServer: false,
      env: { UPSTREAM_PORT: "8030", PROXY_PORT: "8031" },
    },
    {
      command: "npm run build && npx next start --port 3030",
      url: "http://localhost:3030",
      reuseExistingServer: false,
      timeout: 300_000,
      env: { API_URL: "http://localhost:8031" },
    },
  ],
});
