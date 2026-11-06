import type { Availability } from "./api";

/** A recomputed baseline change: both weeks recomputed with today's data (never the number shown then). */
export interface ProjectionChange {
  label: string;
  season: number;
  since_week: number;
  week: number;
  available: boolean;
  reason: string | null;
  baseline_change?: number;
  summary: string;
  explanation: string[];
  served: { week: number; points: number | null; availability: Availability | null };
}

export const CHANGE_NOTE =
  "The change compares two baseline projections, both recomputed now with today's stats and scoring rules, " +
  "so it is not a record of the number shown back then. Each game played in between moves the weighted " +
  "average toward that game's points; a week without a game changes nothing. Injuries, role, matchup and " +
  "news are not in the baseline, so they never explain the change. Availability (a bye, Out or IR) can set " +
  "the served projection to 0 separately.";

/** The comparison window from the page URL (`season`, `since`, `week`), passed on to /api/start-sit.
 *  `season` alone is the usage window's (compare page), so it counts only with a comparison week. */
export function changeQuery(sp: Record<string, string | string[] | undefined>): string {
  if (typeof sp.since !== "string" && typeof sp.week !== "string") return "";
  const keep = (
    [
      ["season", "season"],
      ["since", "since_week"],
      ["week", "week"],
    ] as const
  ).flatMap(([from, to]) => {
    const v = sp[from];
    return typeof v === "string" && /^\d{1,4}$/.test(v) ? [`${to}=${v}`] : [];
  });
  return keep.length ? `&${keep.join("&")}` : "";
}

/** Column header: the weeks compared, always labelled as recomputed. */
export function changeHeader(changes: (ProjectionChange | undefined)[]): string {
  const c = changes.find((x) => x?.available) ?? changes.find(Boolean);
  if (!c) return "Baseline change (recomputed)";
  return c.week === c.served.week
    ? `Baseline vs wk ${c.since_week} (recomputed)`
    : `Baseline wk ${c.since_week}→${c.week} (recomputed)`;
}

/** The cell: the change (computed in Python, only formatted here), its reason, and any availability override. */
export function formatChange(c?: ProjectionChange): { value: string; note: string; served: string | null } {
  if (!c) return { value: "–", note: "", served: null };
  const a = c.served.availability;
  const served =
    a && !a.playable && c.served.points !== null
      ? `served ${c.served.points.toFixed(1)}: ${a.status}${c.week !== c.served.week ? " (now)" : ""}`
      : null;
  if (!c.available || c.baseline_change === undefined) {
    return { value: "comparison unavailable", note: c.reason ?? "", served };
  }
  const d = c.baseline_change;
  const value = d === 0 ? "no change" : `${d > 0 ? "+" : "−"}${Math.abs(d).toFixed(2)}`;
  return { value, note: c.summary, served };
}
