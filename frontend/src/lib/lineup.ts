// One-line summary of the real lineup moves. A sit with no start (e.g. a player on
// bye whose slot is better left empty) is still a change.
export function movesSummary(changes: { start: string[]; sit: string[] }): string {
  if (changes.start.length === 0 && changes.sit.length === 0) {
    return "No changes: your current lineup is already the best by projection.";
  }
  return `Start: ${changes.start.join(", ") || "nobody"} · Sit: ${changes.sit.join(", ") || "nobody"}`;
}

// When the app last asked Sleeper for rosters (checked) vs when this league's roster data
// last changed: an unchanged roster keeps its old changed time on every check.
export interface RostersAsOf {
  week: number;
  synced_at: string;
  checked_at?: string | null;
}

export function rostersLabel(r: RostersAsOf): string {
  const at = (iso: string) => new Date(iso).toLocaleString();
  const changed = `last changed ${at(r.synced_at)}`;
  return r.checked_at ? `rosters checked ${at(r.checked_at)} · ${changed}` : `rosters ${changed}`;
}
