import { expect, test } from "@playwright/test";

import { parseEvents } from "../src/lib/chat";

// One real start/sit question (Claude API, ~2-3 cents; the API caps the run at $0.10).
test("live start/sit question: progress, then a validated answer with clickable sources", async ({ page }) => {
  test.skip(process.env.LIVE_LLM !== "1", "paid: set LIVE_LLM=1 to run");
  await page.goto("/chat");
  const league = page.getByLabel("League");
  await league.selectOption({ index: 1 }); // the first imported league
  await page.getByRole("textbox", { name: "Question" }).fill("bucky irving or jaylen warren this week?");
  const stream = page.waitForResponse((r) => r.url().endsWith("/api/chat"));
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.getByLabel("Progress").getByRole("listitem").first()).toBeVisible();
  const outcome = page.getByLabel("Answer").or(page.getByLabel("Withheld")).or(page.getByRole("alert").filter({ hasText: /./ }));
  await expect(outcome.first()).toBeVisible({ timeout: 90_000 });
  const { events } = parseEvents((await (await stream).text()) + "\n\n");
  const done = events.find((e) => e.event === "done");
  console.log("PAID_RUN", JSON.stringify({ terminal: events.at(-2)?.event, done: done?.data }));
  await expect(page.getByLabel("Answer")).toBeVisible();
  await expect(page.getByLabel("Answer").getByLabel("Sources").getByRole("link").first()).toBeVisible();
});
