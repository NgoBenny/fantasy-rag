import { expect, test } from "@playwright/test";

test("compare two players with real stats", async ({ page, request }) => {
  await page.goto("/players?q=amon-ra");
  await page.getByRole("link", { name: "Compare Amon-Ra St. Brown" }).click();
  await expect(page.getByText("1 picked for comparison")).toBeVisible();
  await page.getByLabel("Player name").fill("puka nacua");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("link", { name: "Compare Puka Nacua" }).click();
  await page.getByRole("link", { name: "Compare 2 players" }).click();

  const table = page.getByRole("table", { name: "Player comparison" });
  await expect(table).toContainText("Amon-Ra St. Brown");
  await expect(table).toContainText("Puka Nacua");

  // The page shows exactly the numbers the API computed.
  const ids = new URL(page.url()).searchParams.get("ids");
  const api = await (await request.get(`http://localhost:8010/api/compare?ids=${ids}&scoring=ppr`)).json();
  const totals = table.getByRole("row", { name: /Total pts \(PPR\)/ });
  for (const p of api.players) {
    await expect(totals).toContainText(String(p.total_points));
  }
});

test("empty, not-found and lineup states", async ({ page }) => {
  await page.goto("/compare");
  await expect(page.getByText("Pick 2–4 players")).toBeVisible();

  await page.goto("/players?q=zzzzqqq");
  await expect(page.getByText(/No fantasy players match/)).toBeVisible();

  // With streaming (loading.tsx) Next serves not-found pages as a soft 404 (HTTP 200).
  await page.goto("/players/not-a-player");
  await expect(page.getByText("This page could not be found.")).toBeVisible();

  await page.goto("/lineup");
  await expect(page.getByText("Pick a league.")).toBeVisible();
});

test("lineup shows moves, recommended slots and the bench", async ({ page }) => {
  await page.goto("/lineup?league=4&roster=1");
  await expect(page.getByText(/^(Start: |No changes)/)).toBeVisible();
  await expect(page.getByRole("table", { name: "Recommended lineup" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Bench" })).toBeVisible();
  await expect(page.getByText(/project 0 and never start/)).toBeVisible();
});

test("Try again recovers once the API is back", async ({ page, request }) => {
  await request.get("http://localhost:8011/__down");
  try {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Something went wrong" })).toBeVisible();
  } finally {
    await request.get("http://localhost:8011/__up");
  }
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText(/stats through week/)).toBeVisible();
});
