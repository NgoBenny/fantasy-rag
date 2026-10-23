import { expect, test } from "@playwright/test";

// Demo mode (playwright.demo.config.ts): sample data, no database, no API key, no .env.
const PROXY = "http://localhost:8031";
const START_SIT = "bucky irving or jaylen warren this week?";

test.afterEach(async ({ request }) => {
  await request.get(`${PROXY}/__up`);
});

test("every page shows the demo label and the data status", async ({ page }) => {
  for (const path of ["/", "/players", "/compare", "/chat", "/league", "/model"]) {
    await page.goto(path);
    await expect(page.getByRole("note")).toContainText("Demo: sample data, not live");
    await expect(page.getByLabel("Data status")).toContainText(/season · stats available through week \d+/);
    await expect(page.getByLabel("Data status")).toContainText("adds/drops:");
  }
});

test("a sample question streams progress, then one answer card with clickable sources", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "How many PPR points did Bijan Robinson score in week 3?" }).click();
  await expect(page.getByLabel("Progress").getByRole("listitem").first()).toBeVisible();
  const answer = page.getByLabel("Answer");
  await expect(answer).toBeVisible();
  await expect(page.getByLabel("Answer")).toHaveCount(1);
  const source = answer.getByLabel("Sources").getByRole("link").first();
  await expect(source).toHaveAttribute("href", /^\/players\//);
  await source.click();
  await expect(page).toHaveURL(/\/players\/00-/);
});

test("news sources open the story", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "Who is kicking for the Rams now?" }).click();
  const link = page.getByLabel("Answer").getByLabel("Sources").getByRole("link").first();
  await expect(link).toHaveAttribute("href", /^https:\/\//);
  await expect(link).toHaveAttribute("target", "_blank");
});

test("start/sit question (recorded answer)", async ({ page }) => {
  const samples = await (await page.request.get(`${PROXY}/api/chat/samples`)).json();
  test.skip(
    !samples.some((s: { question: string }) => s.question === START_SIT),
    "the demo start/sit answer is recorded only after the paid recording is approved",
  );
  await page.goto("/chat");
  await page.getByRole("button", { name: START_SIT }).click();
  await expect(page.getByLabel("Progress").getByRole("listitem").first()).toBeVisible();
  const source = page.getByLabel("Answer").getByLabel("Sources").getByRole("link", { name: /Projections/ });
  const href = (await source.getAttribute("href"))!;
  expect(href).toMatch(/^\/compare\?ids=[^&]+,[^&]+&scoring=(ppr|half|std)$/);
  await source.click();
  // The source opens the same players' projections in the same scoring.
  const ids = new URL(href, "http://x").searchParams.get("ids")!.split(",");
  const scoring = new URL(href, "http://x").searchParams.get("scoring")!;
  const api = await (await page.request.get(`${PROXY}/api/start-sit?ids=${ids.join(",")}&scoring=${scoring}`)).json();
  const table = page.getByRole("table", { name: "Start/sit" });
  for (const r of api.ranked) await expect(table).toContainText(r.player.name);
  await expect(page.getByText(/Higher projection:/)).toContainText(`· ${scoring}`);
});

test("scoring toggles change the points shown", async ({ page }) => {
  const players = await (await page.request.get(`${PROXY}/api/players?q=bijan`)).json();
  const totals: Record<string, string> = {};
  for (const s of ["ppr", "half", "std"]) {
    await page.goto(`/players/${players[0].id}?scoring=${s}`);
    const line = page.getByText(/pts \(.* per game, /);
    await expect(line).toContainText(`, ${s})`);
    totals[s] = (await line.textContent())!;
  }
  expect(new Set(Object.values(totals)).size).toBe(3);
});

test("league-free compare shows next week's projections in the chosen scoring", async ({ page }) => {
  const players: { id: string; name: string }[] = await (await page.request.get(`${PROXY}/api/players`)).json();
  const pick = ["Jaylen Warren", "Bucky Irving"].map((n) => players.find((p) => p.name === n)!.id);
  await page.goto(`/compare?ids=${pick.join(",")}&scoring=std`);
  await expect(page.getByText(/Higher projection:/)).toContainText("· std");
  const api = await (await page.request.get(`${PROXY}/api/start-sit?ids=${pick.join(",")}&scoring=std`)).json();
  const rows = page.getByRole("table", { name: "Start/sit" }).getByRole("row");
  await expect(rows.nth(1)).toContainText(api.ranked[0].player.name);
  await expect(rows.nth(1)).toContainText(String(api.ranked[0].projected_points));
});

test("a stream cut off before its answer shows an error with Retry", async ({ page }) => {
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: 'id: 1\nevent: progress\ndata: {"demo":true,"stage":"context","message":"Reading","elapsed_ms":0}\n\n',
    }),
  );
  await page.goto("/chat");
  await page.getByRole("button", { name: "How many PPR points did Bijan Robinson score in week 3?" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "connection was lost" });
  await expect(alert.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("a clarification shows the failed lookup as an unlinked source", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "is mitchell playing this week" }).click();
  const answer = page.getByLabel("Answer");
  await expect(answer.getByText("needs more detail")).toBeVisible();
  await expect(answer.getByLabel("Sources")).toContainText("Lookup failed");
  await expect(answer.getByLabel("Sources").getByRole("link")).toHaveCount(0);
});

test("a withheld answer shows only the fixed message", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: /an answer that fails verification/ }).click();
  await expect(page.getByLabel("Withheld")).toContainText("I couldn't match every number and source");
  await expect(page.getByLabel("Answer")).toHaveCount(0);
});

test("an outage shows an error, and Retry works once the API is back", async ({ page, request }) => {
  await page.goto("/chat");
  await request.get(`${PROXY}/__down`);
  await page.getByRole("button", { name: "How many PPR points did Bijan Robinson score in week 3?" }).click();
  // Next's route announcer is also role=alert; pick the chat's own error by its text.
  const alert = page.getByRole("alert").filter({ hasText: "unreachable" });
  await expect(alert).toContainText("unreachable");
  await request.get(`${PROXY}/__up`);
  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByLabel("Answer")).toBeVisible();
  await expect(page.getByText("The API is unreachable right now.")).toHaveCount(0);
});

test("an unknown question gets the honest demo fallback", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("textbox", { name: "Question" }).fill("who wins the super bowl?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByLabel("Answer")).toContainText("The demo answers only these sample questions");
});

test("league selection, lineup with bye badge, start/sit projections and waivers", async ({ page }) => {
  await page.goto("/league");
  await page.getByLabel("Leagues").getByRole("link", { name: /Demo League/ }).click();
  await expect(page).toHaveURL(/league=900/);
  await expect(page.getByRole("table", { name: "Recommended lineup" })).toBeVisible();
  await expect(page.getByText("Demo League rules (half PPR)").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Bench" }).getByText("bye")).toBeVisible();

  await page.getByLabel("Views").getByRole("link", { name: "Start/sit" }).click();
  await page.getByLabel("Roster players").getByRole("link", { name: "A.J. Brown" }).click();
  await page.getByLabel("Roster players").getByRole("link", { name: "Puka Nacua" }).click();
  const table = page.getByRole("table", { name: "Start/sit" });
  await expect(table).toBeVisible();
  await expect(table.getByText("Questionable")).toBeVisible();
  await expect(page.getByText(/Higher projection:/)).toBeVisible();
  // The Demo League scores a rule we can't compute: the projections say so.
  await expect(page.getByLabel("Unscored rules")).toContainText("rec_0_4");

  await page.getByLabel("Views").getByRole("link", { name: "Waivers" }).click();
  await expect(page.getByLabel("Availability")).toContainText("availability: sample");
  await expect(page.getByRole("table", { name: "Waivers" })).toBeVisible();
  await expect(page.getByLabel("Unscored rules")).toContainText("rec_0_4");
});

test("any 2-4 demo roster players can be compared, not just recorded pairs", async ({ page }) => {
  await page.goto("/league?league=900&roster=1&view=startsit");
  // Slow every later page load, so each click lands while the previous one is still loading.
  await page.route("**/league?**", async (route) => {
    await new Promise((r) => setTimeout(r, 800));
    await route.continue();
  });
  for (const name of ["Brock Purdy", "Josh Allen", "Trey McBride"])
    await page.getByLabel("Roster players").getByRole("link", { name }).click();
  await expect(page).toHaveURL(/ids=[^&]+(,|%2C)[^&]+(,|%2C)[^&]+/);
  const rows = page.getByRole("table", { name: "Start/sit" }).getByRole("row");
  await expect(rows).toHaveCount(4); // header + 3 players
  await expect(rows.last()).toContainText("Josh Allen"); // on bye: 0, ranked last
  await expect(rows.last()).toContainText("bye");
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
});

test("model performance shows the frozen benchmark, ranges, FantasyPros and the serving policy", async ({ page }) => {
  await page.goto("/model");
  await expect(page.getByLabel("Serving policy")).toContainText("TE: Baseline");
  await expect(page.getByLabel("Serving policy")).toContainText("Ridge qualified for TE");
  const bench = page.getByRole("table", { name: "Benchmark" });
  await expect(bench.getByRole("row")).toHaveCount(1 + 4 * 3); // header + 4 positions x 3 policies
  await expect(page.getByLabel("Promotion")).toContainText("TE: Ridge qualifies");
  await expect(page.getByRole("table", { name: "Range coverage" })).toContainText("pts wide");
  await expect(page.getByRole("table", { name: "FantasyPros" })).toContainText("FantasyPros consensus");
  await expect(page.getByText("captured on Fridays")).toBeVisible();
  await expect(page.getByRole("table", { name: "Model runs" })).toBeVisible();
  // Forward results: honest empty state until a forward week is final.
  await expect(page.getByLabel("Forward results")).toContainText("No finalized weeks yet");
});

test("usage context: start/sit and waivers show it beside the projection, players have a usage section", async ({ page, request }) => {
  await page.goto("/league?league=900&roster=1&view=startsit");
  await page.getByLabel("Roster players").getByRole("link", { name: "A.J. Brown" }).click();
  await page.getByLabel("Roster players").getByRole("link", { name: "Puka Nacua" }).click();
  const table = page.getByRole("table", { name: "Start/sit" });
  await expect(table.getByRole("columnheader", { name: "Usage context (last 3 games)" })).toBeVisible();
  await expect(table).toContainText(/snaps .*%/);
  await expect(page.getByText("Ranked by projection; usage is shown as context only.")).toBeVisible();

  await page.goto("/league?league=900&view=waivers");
  const waivers = page.getByRole("table", { name: "Waivers" });
  await expect(waivers.getByRole("columnheader", { name: "Usage context (last 3 games)" })).toBeVisible();
  await expect(page.getByText("How usage is measured")).toBeVisible();

  const players: { id: string; name: string }[] = await (await request.get(`${PROXY}/api/players?q=gibbs`)).json();
  const gibbs = players.find((p) => p.name === "Jahmyr Gibbs")!;
  const stats = await (await request.get(`${PROXY}/api/players/${gibbs.id}?scoring=ppr`)).json();
  await page.goto(`/players/${gibbs.id}#usage`);
  const usage = page.getByRole("table", { name: "Usage by game" });
  await expect(usage).toBeVisible();
  // The page shows the API's own numbers: the latest game's target share.
  const last = stats.usage.games.at(-1);
  await expect(usage.getByRole("row").last()).toContainText(`${last.target_share_pct.toFixed(1)}%`);
  await expect(page.getByText(/completed games through week \d+/)).toBeVisible();
});

test("waiver position tabs filter the list, keep league and team in the URL, and show empty results", async ({ page }) => {
  await page.goto("/league?league=900&roster=1&view=waivers");
  await expect(page.getByLabel("Waiver list")).toHaveText(
    "Highest projected points across positions (not ranked for your roster)",
  );
  const tabs = page.getByLabel("Positions");
  await tabs.getByRole("link", { name: "WR", exact: true }).click();
  await expect(page).toHaveURL(/league=900&roster=1&view=waivers&pos=WR/);
  await expect(page.getByLabel("Waiver list")).toHaveText("Unrostered WRs by projected points");
  const rows = page.getByRole("table", { name: "Waivers" }).locator("tbody tr");
  await expect(rows).not.toHaveCount(0);
  for (const pos of await rows.locator("td:nth-child(2)").allTextContents()) expect(pos).toBe("WR");
  // Warnings stay visible under every filter.
  await expect(page.getByLabel("Availability")).toBeVisible();
  await expect(page.getByLabel("Unscored rules")).toContainText("rec_0_4");

  await tabs.getByRole("link", { name: "QB", exact: true }).click();
  await expect(page).toHaveURL(/pos=QB/);
  await expect(page.getByRole("table", { name: "Waivers" })).toContainText(
    "No unrostered QB with a projection this week.",
  );
  await expect(page.getByLabel("Availability")).toBeVisible();

  await tabs.getByRole("link", { name: "K", exact: true }).click();
  await expect(page).toHaveURL(/pos=K$/);
  await expect(page.getByLabel("Waiver list")).toHaveText("Unrostered kickers by projected points");

  await tabs.getByRole("link", { name: "All", exact: true }).click();
  await expect(page).toHaveURL(/view=waivers$/);
  await expect(rows).not.toHaveCount(0);
});
