import { describe, expect, it } from "vitest";

import { movesSummary, rostersLabel } from "./lineup";

describe("movesSummary", () => {
  it("reports no changes only when nothing starts or sits", () => {
    expect(movesSummary({ start: [], sit: [] })).toMatch(/^No changes/);
  });
  it("shows a sit-only recommendation", () => {
    expect(movesSummary({ start: [], sit: ["Josh Jacobs"] })).toBe("Start: nobody · Sit: Josh Jacobs");
  });
  it("shows starts and sits", () => {
    expect(movesSummary({ start: ["A", "B"], sit: ["C"] })).toBe("Start: A, B · Sit: C");
  });
});

describe("rostersLabel", () => {
  const changed = "2026-10-20T21:27:21Z";
  const checked = "2026-10-21T21:01:02Z";
  it("shows when rosters were checked apart from when they last changed", () => {
    const label = rostersLabel({ week: 7, synced_at: changed, checked_at: checked });
    expect(label).toBe(
      `rosters checked ${new Date(checked).toLocaleString()} · last changed ${new Date(changed).toLocaleString()}`,
    );
  });
  it("never calls the change time a check when no check is recorded", () => {
    expect(rostersLabel({ week: 7, synced_at: changed })).toBe(`rosters last changed ${new Date(changed).toLocaleString()}`);
  });
});
