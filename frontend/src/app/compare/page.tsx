import Link from "next/link";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SHOWN_STATS, api, scoringOf } from "@/lib/api";

import { StartSitTable } from "../league/lineup-view";

export default async function Compare({ searchParams }: PageProps<"/compare">) {
  const sp = await searchParams;
  const ids = (typeof sp.ids === "string" ? sp.ids.split(",") : []).filter(Boolean).slice(0, 4);
  const scoring = scoringOf(sp.scoring);

  if (ids.length < 2) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold">Compare</h1>
        <p className="text-sm text-muted-foreground">
          Pick 2–4 players on the <Link href="/players" className="underline">Players</Link> page.
        </p>
      </div>
    );
  }

  const [{ players }, projections] = await Promise.all([
    api.compare(ids, scoring),
    api.startSit(ids, null, scoring),
  ]);
  const rows: [string, (p: (typeof players)[number]) => string | number][] = [
    ["Games", (p) => p.games],
    [`Total pts (${scoring.toUpperCase()})`, (p) => p.total_points],
    ["Pts per game", (p) => p.points_per_game ?? "–"],
    ...SHOWN_STATS.filter(([k]) => players.some((p) => p.totals[k])).map(
      ([k, label]) => [label, (p: (typeof players)[number]) => p.totals[k] ?? 0] as [string, (p: (typeof players)[number]) => number],
    ),
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Compare · {players[0].season} season</h1>
        <div className="flex gap-3 text-sm">
          {(["ppr", "half", "std"] as const).map((s) => (
            <Link
              key={s}
              href={`/compare?ids=${ids.join(",")}&scoring=${s}`}
              className={s === scoring ? "font-semibold underline" : "text-muted-foreground hover:underline"}
            >
              {s.toUpperCase()}
            </Link>
          ))}
        </div>
      </div>
      <section className="space-y-2">
        <h2 className="font-semibold">Next week&apos;s projections</h2>
        <StartSitTable result={projections} />
      </section>
      <h2 className="font-semibold">Season stats</h2>
      <Table aria-label="Player comparison">
        <TableHeader>
          <TableRow>
            <TableHead />
            {players.map((p) => (
              <TableHead key={p.player.id} className="text-right">
                <Link href={`/players/${p.player.id}`} className="hover:underline">
                  {p.player.name}
                </Link>
                <div className="text-xs font-normal">
                  {p.player.position} · {p.player.team}
                </div>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map(([label, get]) => (
            <TableRow key={label}>
              <TableCell className="font-medium">{label}</TableCell>
              {players.map((p) => (
                <TableCell key={p.player.id} className="text-right tabular-nums">
                  {get(p)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
