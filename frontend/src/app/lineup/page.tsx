import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";

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
                        {changed && s.player && (
                          <Badge className="ml-2" variant="secondary">
                            change
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{s.player?.projected_points ?? "–"}</TableCell>
                      <TableCell className="text-muted-foreground">{s.current?.name ?? "–"}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
          <p className="text-xs text-muted-foreground">
            Model {lineup.model_version}: weighted recent games, assumes everyone plays (no injury or bye
            awareness yet).
            {lineup.unhandled_slots.length > 0 && ` Not optimized: ${lineup.unhandled_slots.join(", ")}.`}
            {lineup.no_projection.length > 0 && ` No projection: ${lineup.no_projection.join(", ")}.`}
          </p>
        </>
      )}
    </div>
  );
}
