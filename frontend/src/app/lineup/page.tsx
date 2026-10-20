import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { type LineupPlayer, api } from "@/lib/api";

function Status({ p }: { p: LineupPlayer | null }) {
  const a = p?.availability;
  if (!a) return null;
  return (
    <Badge variant={a.playable ? "outline" : "destructive"} className="ml-2" title={a.note ?? a.source}>
      {a.status}
    </Badge>
  );
}

export default async function LineupPage({ searchParams }: PageProps<"/lineup">) {
  const sp = await searchParams;
  const leagueId = Number(sp.league) || null;
  const rosterId = Number(sp.roster) || null;
  const leagues = await api.leagues();

  if (leagues.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No leagues imported yet. Run <code>uv run fantasy-rag league import</code>.
      </p>
    );
  }
  const league = leagues.find((l) => l.id === leagueId);
  const lineup = league && rosterId ? await api.lineup(league.id, rosterId) : null;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Lineup optimizer</h1>
      <div className="flex flex-wrap gap-3 text-sm">
        {leagues.map((l) => (
          <Link
            key={l.id}
            href={`/lineup?league=${l.id}`}
            className={l.id === leagueId ? "font-semibold underline" : "hover:underline"}
          >
            {l.name ?? `League ${l.id}`} ({l.rosters.length} teams)
          </Link>
        ))}
      </div>

      {league && (
        <div className="flex flex-wrap gap-2 text-sm">
          {league.rosters.map((r) => (
            <Link
              key={r.roster_id}
              href={`/lineup?league=${league.id}&roster=${r.roster_id}`}
              className={`rounded border px-2 py-1 ${r.roster_id === rosterId ? "bg-muted font-semibold" : ""}`}
            >
              {r.owner_name ?? `Roster ${r.roster_id}`}
            </Link>
          ))}
        </div>
      )}

      {!league ? (
        <p className="text-sm text-muted-foreground">Pick a league.</p>
      ) : !lineup ? (
        <p className="text-sm text-muted-foreground">Pick a team.</p>
      ) : (
        <>
          <p className="text-sm" aria-label="Lineup changes">
            {lineup.changes.start.length === 0
              ? "No changes: your current lineup is already the best by projection."
              : `Start: ${lineup.changes.start.join(", ")} · Sit: ${lineup.changes.sit.join(", ") || "nobody"}`}
          </p>
          <p className="text-sm">
            Week {lineup.week} · {lineup.scoring} · projected <b>{lineup.recommended_total}</b> vs current{" "}
            {lineup.current_total} (<b>{lineup.gain >= 0 ? "+" : ""}{lineup.gain}</b>) · rosters synced{" "}
            {new Date(lineup.rosters_as_of.synced_at).toLocaleString()}
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
      )}
    </div>
  );
}
