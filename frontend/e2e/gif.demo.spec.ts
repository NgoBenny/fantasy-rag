import { mkdirSync } from "node:fs";

import { type Page, expect, test } from "@playwright/test";

// Frames for docs/demo.gif, from DEMO_MODE only (sample data, no personal data).
// RECORD_GIF=1 npx playwright test -c playwright.demo.config.ts gif.demo
// then: uv run python ../scripts/make_gif.py  (from backend/)
const OUT = "test-results/gif";
let n = 0;

async function frame(page: Page, ms: number) {
  await page.screenshot({ path: `${OUT}/${String(n++).padStart(3, "0")}_${ms}.png` });
}

test("record demo GIF frames", async ({ page }) => {
  test.skip(process.env.RECORD_GIF !== "1", "set RECORD_GIF=1 to record docs/demo.gif frames");
  test.setTimeout(120_000);
  mkdirSync(OUT, { recursive: true });
  await page.setViewportSize({ width: 1100, height: 760 });

  await page.goto("/chat");
  await frame(page, 1500);
  await page.getByRole("button", { name: "bucky irving or jaylen warren this week?" }).click();
  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(350);
    await frame(page, 350); // progress lines arriving
  }
  await expect(page.getByLabel("Answer")).toBeVisible();
  await page.getByLabel("Answer").scrollIntoViewIfNeeded();
  await frame(page, 3500); // the validated answer with numbered sources

  const players: { id: string; name: string }[] = await (await page.request.get("http://localhost:8031/api/players")).json();
  const ids = ["Jaylen Warren", "Bucky Irving"].map((name) => players.find((p) => p.name === name)!.id);
  await page.goto(`/compare?ids=${ids.join(",")}&scoring=ppr`);
  await expect(page.getByRole("table", { name: "Start/sit" })).toBeVisible();
  await frame(page, 3500); // the source's destination: projections with 80% ranges

  await page.goto("/league?league=900&roster=1&view=lineup");
  await expect(page.getByRole("table", { name: "Recommended lineup" })).toBeVisible();
  await frame(page, 3000);

  await page.goto("/model");
  await expect(page.getByRole("table", { name: "Benchmark" })).toBeVisible();
  await frame(page, 3500); // the frozen benchmark
  await page.getByRole("table", { name: "Range coverage" }).scrollIntoViewIfNeeded();
  await frame(page, 3500); // ranges and the FantasyPros comparison
});
