import { describe, expect, it } from "vitest";

import { movesSummary } from "./lineup";

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
