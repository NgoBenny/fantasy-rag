import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";

const POSITIONS = ["", "QB", "RB", "WR", "TE", "K"];

// Search state and the compare picks live in the URL, so this page needs no client JS.
export default async function Players({ searchParams }: PageProps<"/players">) {
  const sp = await searchParams;
  const q = typeof sp.q === "string" ? sp.q : "";
  const position = typeof sp.position === "string" ? sp.position : "";
  const picks = (typeof sp.pick === "string" ? sp.pick.split(",") : []).filter(Boolean).slice(0, 4);
  const hits = q.trim().length >= 2 ? await api.search(q, position || undefined) : [];

  const withPick = (id: string) => {
    const next = picks.includes(id) ? picks.filter((p) => p !== id) : [...picks, id].slice(0, 4);
    return `/players?${new URLSearchParams({ q, position, pick: next.join(",") })}`;
  };

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Players</h1>
      <form className="flex gap-2" action="/players">
        <Input name="q" defaultValue={q} placeholder="Search by name, e.g. Amon-Ra" aria-label="Player name" />
        <select
          name="position"
          defaultValue={position}
          aria-label="Position"
          className="rounded-md border bg-background px-2 text-sm"
        >
          {POSITIONS.map((p) => (
            <option key={p} value={p}>
              {p || "All"}
            </option>
          ))}
        </select>
        <input type="hidden" name="pick" value={picks.join(",")} />
        <Button type="submit">Search</Button>
      </form>

      {picks.length > 0 && (
        <div className="flex items-center gap-3 text-sm">
          <span>{picks.length} picked for comparison</span>
          {picks.length >= 2 ? (
            <Link href={`/compare?ids=${picks.join(",")}`} className={buttonVariants({ size: "sm" })}>
              Compare {picks.length} players
            </Link>
          ) : (
            <span className="text-muted-foreground">pick one more</span>
          )}
        </div>
      )}

      {q.trim().length < 2 ? (
        <p className="text-sm text-muted-foreground">Type at least two letters of a player&apos;s name.</p>
      ) : hits.length === 0 ? (
        <p className="text-sm text-muted-foreground">No fantasy players match &ldquo;{q}&rdquo;.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Player</TableHead>
              <TableHead>Pos</TableHead>
              <TableHead>Team</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {hits.map((p) => (
              <TableRow key={p.id}>
                <TableCell>
                  <Link href={`/players/${p.id}`} className="hover:underline">
                    {p.name}
                  </Link>
                  {!p.active && (
                    <Badge variant="outline" className="ml-2">
                      no stats this season
                    </Badge>
                  )}
                </TableCell>
                <TableCell>{p.position}</TableCell>
                <TableCell>{p.team}</TableCell>
                <TableCell className="text-right">
                  <Link href={withPick(p.id)} className="text-sm hover:underline">
                    {picks.includes(p.id) ? "Remove from compare" : `Compare ${p.name}`}
                  </Link>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
