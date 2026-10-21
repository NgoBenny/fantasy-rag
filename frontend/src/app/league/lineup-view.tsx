import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { Lineup, LineupPlayer, StartSit } from "@/lib/api";
import { movesSummary, rostersLabel } from "@/lib/lineup";

/** Rules a projection leaves out; without this, incomplete projections look complete. */
export function UnscoredNote({ items }: { items: { unscored_rules: string[] }[] }) {
  const rules = [...new Set(items.flatMap((i) => i.unscored_rules))].sort();
  if (rules.length === 0) return null;
  return (
    <p className="text-sm" aria-label="Unscored rules">
      Projections leave out league rules we can&apos;t compute yet: {rules.join(", ")}.
    </p>
  );
}

/** The 80% range from the serving policy's own past errors (nominal; coverage is measured). */
export function Range({ r }: { r?: [number, number] | null }) {
  return r ? <>{r[0]} to {r[1]}</> : <>–</>;
}

export function StartSitTable({ result }: { result: StartSit }) {
  return (
    <>
      <p className="text-sm">
        Higher projection: <b>{result.higher_projection ?? "none (no projection)"}</b> · {result.ranked[0]?.scoring}
      </p>
      <UnscoredNote items={result.ranked} />
      <Table aria-label="Start/sit">
        <TableHeader>
          <TableRow>
            <TableHead>Player</TableHead>
            <TableHead className="text-right">Proj (wk {result.ranked[0]?.week})</TableHead>
            <TableHead className="text-right">80% range</TableHead>
            <TableHead className="text-right">Last game</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {result.ranked.map((r) => (
            <TableRow key={r.player.id}>
              <TableCell>
                {r.player.name}
                <Status p={{ ...r.player, projected_points: r.projected_points, availability: r.availability }} />
              </TableCell>
              <TableCell className="text-right tabular-nums">{r.projected_points ?? "–"}</TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                <Range r={r.range_80} />
              </TableCell>
              <TableCell className="text-right tabular-nums">{r.last_game?.points ?? "–"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  );
}

export function Status({ p }: { p: LineupPlayer | null }) {
  const a = p?.availability;
  if (!a) return null;
  return (
    <Badge variant={a.playable ? "outline" : "destructive"} className="ml-2" title={a.note ?? a.source}>
      {a.status}
    </Badge>
  );
}

export function LineupView({ lineup }: { lineup: Lineup }) {
  return (
    <>
          <p className="text-sm" aria-label="Lineup changes">
            {movesSummary(lineup.changes)}
          </p>
          <p className="text-sm">
            Week {lineup.week} · {lineup.scoring} · projected <b>{lineup.recommended_total}</b> vs current{" "}
            {lineup.current_total} (<b>{lineup.gain >= 0 ? "+" : ""}{lineup.gain}</b>) ·{" "}
            {rostersLabel(lineup.rosters_as_of)}
          </p>
          {lineup.recommended.every((s) => !s.player) ? (
            <p className="text-sm text-muted-foreground">This roster has no players with a projection.</p>
          ) : (
            <Table aria-label="Recommended lineup">
              <TableHeader>
                <TableRow>
                  <TableHead>Slot</TableHead>
                  <TableHead>Recommended</TableHead>
                  <TableHead className="text-right">Proj</TableHead>
                  <TableHead>Current</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {lineup.recommended.map((s, i) => {
                  const changed = s.player?.id !== s.current?.id;
                  return (
                    <TableRow key={i}>
                      <TableCell>{s.slot}</TableCell>
                      <TableCell>
                        {s.player?.name ?? (lineup.unhandled_slots.includes(s.slot) ? "not optimized" : "empty")}
                        <Status p={s.player} />
                        {changed && s.player && (
                          <Badge className="ml-2" variant="secondary">
                            change
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{s.player?.projected_points ?? "–"}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {s.current?.name ?? "–"}
                        <Status p={s.current} />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
          {lineup.bench.length > 0 && (
            <>
              <h2 className="pt-2 text-lg font-semibold">Bench</h2>
              <Table aria-label="Bench">
                <TableHeader>
                  <TableRow>
                    <TableHead>Player</TableHead>
                    <TableHead>Pos</TableHead>
                    <TableHead className="text-right">Proj</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {lineup.bench.map((b) => (
                    <TableRow key={b.id}>
                      <TableCell>
                        {b.name}
                        <Status p={b} />
                      </TableCell>
                      <TableCell>{b.position}</TableCell>
                      <TableCell className="text-right tabular-nums">{b.projected_points ?? "–"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          )}
          <p className="text-xs text-muted-foreground">
            Model {lineup.model_version}: weighted recent games. Players on bye or ruled out (Out, IR) project 0
            and never start; Questionable and Doubtful are flagged but not discounted.
            {lineup.unhandled_slots.length > 0 && ` Not optimized: ${lineup.unhandled_slots.join(", ")}.`}
            {lineup.no_projection.length > 0 && ` No projection: ${lineup.no_projection.join(", ")}.`}
            {lineup.unscored_rules.length > 0 &&
              ` Projections leave out league rules we can't compute yet: ${lineup.unscored_rules.join(", ")}.`}
          </p>
    </>
  );
}
