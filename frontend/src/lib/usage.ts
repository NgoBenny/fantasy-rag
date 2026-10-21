// Usage context (Phase 10): types and plain formatting, shared by the player, compare,
// start/sit and waiver views. Shares arrive as percentages from the API; nothing here
// computes a new number.

export interface UsageGame {
  season: number;
  week: number;
  opponent: string | null;
  snap_pct: number | null;
  targets: number;
  target_share_pct: number | null;
  carries: number;
  carry_share_pct: number | null;
  rz_looks: number | null; // null: no play-by-play for that game (unknown, not 0)
  gl_carries: number | null;
  rz_unknown: number | null;
}

export interface UsageWindow {
  games: number;
  weeks: number[];
  snap_pct: number | null; // null when any game in the window has no snap data
  snap_missing: number;
  snap_pct_approx: boolean;
  target_share_pct: number | null;
  carry_share_pct: number | null;
  carries: number;
  rz_looks: number | null;
  rz_unknown: number;
}

export interface UsageFact {
  kind: string;
  text: string;
}

export interface UsageAsOf {
  season: number;
  through_week: number | null;
  stats_updated_at: string | null;
  snaps_updated_at: string | null;
  pbp_updated_at: string | null;
  updated_at: string | null; // the oldest of the three; null if one never loaded
}

export interface Usage {
  games?: UsageGame[];
  window: UsageWindow | null;
  facts: UsageFact[];
  as_of?: UsageAsOf;
}

export const USAGE_NOTE =
  "Completed regular-season games only. Target (carry) share: the player's targets (carries) ÷ the team's that game; last-3 shares are totals over those games, not averages. Snap share is nflverse's; ≈ marks a last-3 snap share built on an estimated team total. Red-zone looks: targets + carries inside the opponent's 20 (goal-line carries, inside the 5, are part of them). These are observations, not reasons for a projection.";

export function pct(v: number | null | undefined, approx = false): string {
  return v === null || v === undefined ? "–" : `${approx ? "≈" : ""}${v.toFixed(1)}%`;
}

/** Red-zone looks: "—" when unknown; known count, goal-line carries, unknown yard lines. */
export function redZone(looks: number | null, gl?: number | null, unknown?: number | null): string {
  if (looks === null) return "—";
  const parts = [`${looks}${unknown ? "+" : ""}`];
  if (gl) parts.push(`(${gl} goal-line)`);
  if (unknown) parts.push(`(${unknown} play${unknown > 1 ? "s" : ""} with unknown yard line)`);
  return parts.join(" ");
}

/** One line for a table cell: last-3 snap share, target or carry share, red-zone looks. */
export function usageSummary(w: UsageWindow | null): string {
  if (!w) return "No completed games";
  const share =
    w.carries > 0 && (w.carry_share_pct ?? 0) > (w.target_share_pct ?? 0)
      ? `carries ${pct(w.carry_share_pct)}`
      : `targets ${pct(w.target_share_pct)}`;
  const snaps = w.snap_missing
    ? `snaps – (no snap data for ${w.snap_missing} game${w.snap_missing > 1 ? "s" : ""})`
    : `snaps ${pct(w.snap_pct, w.snap_pct_approx)}`;
  return `${snaps} · ${share} · red zone ${redZone(w.rz_looks, null, w.rz_unknown)}`;
}

/** The stats window a page shows, from its search params (only valid numbers pass). */
export function windowQuery(sp: Record<string, string | string[] | undefined>): string {
  const keep = ["season", "start_week", "end_week", "usage_games"].flatMap((k) => {
    const v = sp[k];
    return typeof v === "string" && /^\d{1,4}$/.test(v) ? [`${k}=${v}`] : [];
  });
  return keep.length ? `&${keep.join("&")}` : "";
}
