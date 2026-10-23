import { expect, test } from "@playwright/test";

// Live data, unpaid (playwright.config.ts blanks the Anthropic key).

test("league selection, freshness, waivers availability and start/sit projections", async ({ page }) => {
  test.setTimeout(240_000); // may walk past eliminated (empty) guillotine teams in dev mode
  await page.goto("/league");
  await expect(page.getByLabel("Data status")).toContainText(/season · stats available through week \d+/);
  await expect(page.getByLabel("Data status")).toContainText("adds/drops:");
  await page.getByLabel("Leagues").getByRole("link").first().click();
  await expect(page).toHaveURL(/league=\d+/);

  await page.getByLabel("Views").getByRole("link", { name: "Waivers" }).click();
  await expect(page).toHaveURL(/view=waivers/, { timeout: 30_000 });
  await expect(page.getByLabel("Availability")).toContainText(/availability: (current|stale|uncertain)/);
  await expect(page.getByRole("table", { name: "Waivers" })).toBeVisible();

  await page.getByLabel("Views").getByRole("link", { name: "Start/sit" }).click();
  await expect(page).toHaveURL(/view=startsit/);
  await expect(page.getByLabel("Teams")).toBeVisible();
  // Pick a team with at least two players (an eliminated guillotine team has none).
  const teams = page.getByLabel("Teams").getByRole("link");
  for (let i = 0; i < (await teams.count()); i++) {
    const target = (await teams.nth(i).getAttribute("href")) ?? "";
    await page.goto(target); // wait for this team's page, not the previous one
    await expect(page.getByText(/Pick 2–4 of your players|This team has no players/)).toBeVisible();
    if ((await page.getByLabel("Roster players").getByRole("link").count()) >= 2) break;
  }
  // Page state lives in the URL: read two players' toggle links and open both at once.
  const links = page.getByLabel("Roster players").getByRole("link");
  const pick = async (i: number) =>
    new URL((await links.nth(i).getAttribute("href")) ?? "", "http://x").searchParams.get("ids");
  const url = new URL(page.url());
  url.searchParams.set("ids", `${await pick(0)},${await pick(1)}`);
  await page.goto(url.pathname + url.search);
  await expect(page.getByRole("table", { name: "Start/sit" })).toBeVisible();
  await expect(page.getByText(/Higher projection:/)).toBeVisible();
  await expect(page.getByText(/league \d+ rules/).first()).toBeVisible();

  // A chat source links here with players but no team: the projections still show.
  url.searchParams.delete("roster");
  await page.goto(url.pathname + url.search);
  await expect(page.getByRole("table", { name: "Start/sit" }).getByRole("row")).toHaveCount(3);
});

test("with Claude unavailable, chat says so and stats still work", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("textbox", { name: "Question" }).fill("who should I start at QB?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "unavailable" })).toContainText(
    "Stats, projections and lineups still work",
  );
  await page.goto("/players?q=allen");
  await expect(page.getByRole("link", { name: "Josh Allen", exact: true })).toBeVisible();
  await page.goto("/league");
  await page.getByLabel("Leagues").getByRole("link").first().click();
  await page.getByLabel("Teams").getByRole("link").nth(1).click();
  await expect(page.getByLabel("Lineup changes")).toBeVisible();
});

test("model page: benchmark, ranges and forward runs with honest status", async ({ page }) => {
  await page.goto("/model");
  await expect(page.getByRole("table", { name: "Benchmark" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Range coverage" })).toBeVisible();
  const forward = page.getByRole("table", { name: "Forward runs" });
  await expect(forward).toBeVisible();
  // Week 7's runs were made after its Tuesday deadline: shown as late, never on time.
  await expect(forward.getByRole("row").filter({ hasText: "wk 7" }).first()).toContainText("late (not counted)");
  // Forward results per scoring rules; the selector keeps state in the URL.
  const results = page.getByLabel("Forward results");
  await expect(results).toBeVisible();
  await results.getByRole("link", { name: "HALF" }).click();
  await expect(page).toHaveURL(/scoring=half/);
  await expect(page.getByLabel("Forward results")).toBeVisible();
});

test("start/sit shows each projection's 80% range", async ({ page }) => {
  const players = await (await page.request.get("http://localhost:8012/api/players?q=kelce&position=TE")).json();
  const others = await (await page.request.get("http://localhost:8012/api/players?q=ertz&position=TE")).json();
  await page.goto(`/compare?ids=${players[0].id},${others[0].id}&scoring=ppr`);
  const table = page.getByRole("table", { name: "Start/sit" });
  await expect(table.getByRole("columnheader", { name: "80% range" })).toBeVisible();
  await expect(table.getByRole("row").nth(1)).toContainText(/-?\d+(\.\d+)? to -?\d+(\.\d+)?/);
});

test("a usage citation opens each player's same games at their usage section", async ({ page }) => {
  const API = "http://localhost:8011";
  const find = async (q: string) => (await (await page.request.get(`${API}/api/players?q=${q}`)).json())[0];
  const [a, b] = [await find("gibbs"), await find("worthy")];
  const asOf = (await (await page.request.get(`${API}/api/players/${a.id}`)).json()).usage.as_of;
  const window = `season=${asOf.season}&end_week=${asOf.through_week}&usage_games=4`;
  // The link format get_usage's citation uses (backend: api/chat.py _href, tested there).
  const href = `/compare?ids=${a.id},${b.id}&${window}#usage`;
  const answer = {
    answer: "Usage for both players is in the cited result.", answer_type: "stats", season: asOf.season,
    week: null, scoring_format: null, data_as_of: null, model_version: null,
    facts: [{ text: "usage", source_ids: ["r1"] }], projections: [], citations: ["r1"], limitations: [],
  };
  const sse = (event: string, data: object) => `event: ${event}
data: ${JSON.stringify(data)}

`;
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        sse("answer", { demo: false, status: "ok", answer, sources: [
          { id: "r1", kind: "tool", tool: "get_usage", label: `Usage: ${a.name}, ${b.name}`, href, failed: false },
        ] }) +
        sse("done", { demo: false, query_id: null, logged: false, progress_first_ms: null, answer_ms: null }),
    }),
  );
  await page.goto("/chat");
  await page.getByRole("textbox", { name: "Question" }).fill("how has their usage changed?");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByLabel("Sources").getByRole("link", { name: /^Usage:/ }).click();
  await expect(page).toHaveURL(new RegExp(`usage_games=4#usage$`));
  for (const p of [a, b]) {
    // Each player's own last 4 completed games, exactly as the API gives them for that window.
    const games = (await (await page.request.get(`${API}/api/players/${p.id}?${window}`)).json()).usage.games;
    const table = page.getByRole("table", { name: `Usage by game: ${p.name}` });
    await expect(table.locator("tbody tr td:first-child")).toHaveText(
      games.map((g: { season: number; week: number }) => `${g.season} wk ${g.week}`),
    );
    expect(games.length).toBeLessThanOrEqual(4);
  }
});
