import { describe, expect, it } from "vitest";

import { changeHeader, changeQuery, formatChange, type ProjectionChange } from "./change";

const base: ProjectionChange = {
  label: "recomputed baseline change",
  season: 2026,
  since_week: 8,
  week: 9,
  available: true,
  reason: null,
  baseline_change: 1.62,
  summary: "week 8 game 21.0 pts vs 15.3 before",
  explanation: [],
  served: { week: 9, points: 16.24, availability: null },
};

describe("formatChange", () => {
  it("signs a rise and a drop, and says no change for zero", () => {
    expect(formatChange(base).value).toBe("+1.62");
    expect(formatChange({ ...base, baseline_change: -0.4 }).value).toBe("−0.40");
    expect(formatChange({ ...base, baseline_change: 0 }).value).toBe("no change");
    expect(formatChange(base).note).toBe(base.summary);
  });
  it("shows an unavailable comparison with its reason", () => {
    const got = formatChange({ ...base, available: false, reason: "no games before week 8", baseline_change: undefined });
    expect(got).toEqual({ value: "comparison unavailable", note: "no games before week 8", served: null });
  });
  it("never shows a positive change beside a zero without the availability override", () => {
    const out = { ...base, served: { week: 9, points: 0, availability: { status: "Out", playable: false } } };
    expect(formatChange(out)).toMatchObject({ value: "+1.62", served: "served 0.0: Out" });
    expect(formatChange({ ...out, week: 7, since_week: 6 }).served).toBe("served 0.0: Out (now)");
  });
});

describe("changeHeader and changeQuery", () => {
  it("names the weeks compared and says recomputed", () => {
    expect(changeHeader([base])).toBe("Baseline vs wk 8 (recomputed)");
    expect(changeHeader([{ ...base, since_week: 5, week: 7 }])).toBe("Baseline wk 5→7 (recomputed)");
  });
  it("passes only numeric window params on, under the API's names", () => {
    expect(changeQuery({ season: "2026", since: "5", week: "9", ids: "x" })).toBe("&season=2026&since_week=5&week=9");
    expect(changeQuery({ since: "5;drop" })).toBe("");
    // A usage-window season on /compare (no comparison week) is not a change window.
    expect(changeQuery({ season: "2026", end_week: "8" })).toBe("");
  });
});
