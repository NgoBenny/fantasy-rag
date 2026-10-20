// One-line summary of the real lineup moves. A sit with no start (e.g. a player on
// bye whose slot is better left empty) is still a change.
export function movesSummary(changes: { start: string[]; sit: string[] }): string {
  if (changes.start.length === 0 && changes.sit.length === 0) {
    return "No changes: your current lineup is already the best by projection.";
  }
  return `Start: ${changes.start.join(", ") || "nobody"} · Sit: ${changes.sit.join(", ") || "nobody"}`;
}
