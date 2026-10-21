import { describe, expect, it } from "vitest";

import { pct, redZone, usageSummary, windowQuery } from "./usage";

const w = {
  games: 3, weeks: [4, 5, 6], snap_pct: 80.3, snap_pct_approx: false, target_share_pct: 20.3,
  carry_share_pct: 0, carries: 0, rz_looks: 2, rz_unknown: 0,
};

describe("usage formatting", () => {
  it("marks estimated snap shares and missing values", () => {
    expect(pct(80.3)).toBe("80.3%");
    expect(pct(80.3, true)).toBe("≈80.3%");
    expect(pct(null)).toBe("–");
  });
  it("never shows unknown red-zone looks as 0", () => {
    expect(redZone(null)).toBe("—");
    expect(redZone(0)).toBe("0");
    expect(redZone(5, 2)).toBe("5 (2 goal-line)");
    expect(redZone(3, 0, 1)).toBe("3+ (1 play with unknown yard line)");
  });
  it("summarises a window by its main share", () => {
    expect(usageSummary(w)).toBe("snaps 80.3% · targets 20.3% · red zone 2");
    expect(usageSummary({ ...w, carries: 60, carry_share_pct: 77.9 })).toBe(
      "snaps 80.3% · carries 77.9% · red zone 2",
    );
    expect(usageSummary(null)).toBe("No completed games");
  });
  it("passes through only a valid week window", () => {
    expect(windowQuery({ season: "2026", start_week: "5", end_week: "8" })).toBe(
      "&season=2026&start_week=5&end_week=8",
    );
    expect(windowQuery({ start_week: "5;drop", scoring: "ppr" })).toBe("");
  });
});
