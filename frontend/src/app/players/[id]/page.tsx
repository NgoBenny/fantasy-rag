import Link from "next/link";
import { notFound } from "next/navigation";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { UsageSection } from "@/components/usage";
import { ApiError, SHOWN_STATS, api, scoringOf } from "@/lib/api";
import { windowQuery } from "@/lib/usage";

export default async function PlayerPage({ params, searchParams }: PageProps<"/players/[id]">) {
  const { id } = await params;
  const sp = await searchParams;
  const scoring = scoringOf(sp.scoring);
  const window = windowQuery(sp); // a cited window (e.g. from a chat answer) is kept
  const stats = await api.player(id, scoring, window).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  });
  const cols = SHOWN_STATS.filter(([k]) => stats.totals[k]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">
          {stats.player.name}{" "}
          <span className="text-base text-muted-foreground">
            {stats.player.position} · {stats.player.team}
          </span>
        </h1>
        <div className="flex gap-3 text-sm">
          {(["ppr", "half", "std"] as const).map((s) => (
            <Link
              key={s}
              href={`/players/${id}?scoring=${s}${window}`}
              className={s === scoring ? "font-semibold underline" : "text-muted-foreground hover:underline"}
            >
              {s.toUpperCase()}
            </Link>
          ))}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {stats.season} weeks {stats.weeks[0]}–{stats.weeks[1]} · {stats.games} games ·{" "}
            {stats.total_points} pts ({stats.points_per_game ?? "–"} per game, {stats.scoring})
          </CardTitle>
        </CardHeader>
        <CardContent>
          {stats.by_week.length === 0 ? (
            <p className="text-sm text-muted-foreground">No games this season.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Week</TableHead>
                  <TableHead>Opp</TableHead>
                  <TableHead className="text-right">Pts</TableHead>
                  {cols.map(([k, label]) => (
                    <TableHead key={k} className="text-right">
                      {label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {stats.by_week.map((w) => (
                  <TableRow key={w.week}>
                    <TableCell>{w.week}</TableCell>
                    <TableCell>{w.opponent}</TableCell>
                    <TableCell className="text-right tabular-nums">{w.points}</TableCell>
                    {cols.map(([k]) => (
                      <TableCell key={k} className="text-right tabular-nums">
                        {w[k] ?? 0}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {stats.unscored_rules.length > 0 && (
            <p className="mt-3 text-xs text-muted-foreground">
              Not scored (unsupported): {stats.unscored_rules.join(", ")}
            </p>
          )}
        </CardContent>
      </Card>
      <Card id="usage">
        <CardContent className="pt-6">
          <UsageSection usage={stats.usage} />
        </CardContent>
      </Card>
    </div>
  );
}
