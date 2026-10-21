import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { USAGE_NOTE, type Usage, pct, redZone, usageSummary } from "@/lib/usage";

/** Usage facts, or a plain statement that none fired (never a made-up reason). */
export function UsageFacts({ usage }: { usage: Usage }) {
  if (usage.facts.length === 0) {
    return <p className="text-xs text-muted-foreground">No notable usage change.</p>;
  }
  return (
    <ul className="list-disc pl-4 text-xs text-muted-foreground">
      {usage.facts.map((f) => (
        <li key={f.text}>{f.text}</li>
      ))}
    </ul>
  );
}

/** A table cell: last-3 usage beside a projection. Context, not the reason for the rank. */
export function UsageContext({ usage }: { usage?: Usage | null }) {
  if (!usage) return <>–</>;
  return (
    <div className="space-y-1">
      <div className="text-sm tabular-nums">{usageSummary(usage.window)}</div>
      <UsageFacts usage={usage} />
    </div>
  );
}

export function UsageAsOfLine({ usage }: { usage: Usage }) {
  const a = usage.as_of;
  if (!a) return null;
  const updated = a.stats_updated_at ? new Date(a.stats_updated_at).toLocaleString() : "unknown";
  return (
    <p className="text-xs text-muted-foreground">
      {a.season} season, completed games through week {a.through_week ?? "–"} · data updated {updated}
    </p>
  );
}

export function UsageNote() {
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer">How usage is measured</summary>
      <p className="mt-1">{USAGE_NOTE}</p>
    </details>
  );
}

/** A player's per-game usage for the page's week window, with facts and freshness. */
export function UsageSection({ usage, title }: { usage?: Usage; title?: string }) {
  if (!usage) return null;
  const games = usage.games ?? [];
  return (
    <section className="space-y-2" aria-label={title ? `Usage: ${title}` : "Usage"}>
      <h3 className="font-semibold">{title ? `Usage · ${title}` : "Usage"}</h3>
      <UsageAsOfLine usage={usage} />
      {games.length === 0 ? (
        <p className="text-sm text-muted-foreground">No completed games in this window.</p>
      ) : (
        <Table aria-label={title ? `Usage by game: ${title}` : "Usage by game"}>
          <TableHeader>
            <TableRow>
              <TableHead>Week</TableHead>
              <TableHead>Opp</TableHead>
              <TableHead className="text-right">Snap share</TableHead>
              <TableHead className="text-right">Targets (share)</TableHead>
              <TableHead className="text-right">Carries (share)</TableHead>
              <TableHead className="text-right">Red-zone looks</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {games.map((g) => (
              <TableRow key={g.week}>
                <TableCell>
                  {g.season} wk {g.week}
                </TableCell>
                <TableCell>{g.opponent ?? "–"}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(g.snap_pct)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {g.targets} ({pct(g.target_share_pct)})
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {g.carries} ({pct(g.carry_share_pct)})
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {redZone(g.rz_looks, g.gl_carries, g.rz_unknown)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <p className="text-sm">Last 3 games: {usageSummary(usage.window)}</p>
      <UsageFacts usage={usage} />
      <UsageNote />
    </section>
  );
}
