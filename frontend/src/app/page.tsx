import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";

// Live data on every request; never prerendered at build time (CI has no API).
export const dynamic = "force-dynamic";

const STALE_HOURS = 24;

export default async function Dashboard() {
  const [health, freshness, trending] = await Promise.all([api.health(), api.freshness(), api.trending()]);
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">
        {health.season} season · stats through week {health.latest_week}
      </h1>
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Data freshness</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Source</TableHead>
                  <TableHead>Last successful sync</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {freshness.map((f) => {
                  const ageH = Number(f.age_hours);
                  return (
                    <TableRow key={f.source}>
                      <TableCell className="font-mono text-xs">{f.source}</TableCell>
                      <TableCell className={ageH > STALE_HOURS ? "text-destructive" : undefined}>
                        {new Date(f.last_ok).toLocaleString()}
                        {ageH > STALE_HOURS ? " (stale)" : ""}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Most added on Sleeper</CardTitle>
          </CardHeader>
          <CardContent>
            {trending.players.length === 0 ? (
              <p className="text-sm text-muted-foreground">No trending data yet.</p>
            ) : (
              <ol className="space-y-1 text-sm">
                {trending.players.map((p) => (
                  <li key={p.player_id} className="flex justify-between">
                    <Link href={`/players/${p.player_id}`} className="hover:underline">
                      {p.full_name} <span className="text-muted-foreground">{p.position} {p.team}</span>
                    </Link>
                    <span className="tabular-nums">{p.count.toLocaleString()}</span>
                  </li>
                ))}
              </ol>
            )}
            <p className="mt-3 text-xs text-muted-foreground">Trending data from Sleeper.</p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
