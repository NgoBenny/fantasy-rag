import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { type AvailabilityAsOf, type Lineup, type LineupPlayer, api } from "@/lib/api";

import { LineupView, StartSitTable, Status, UnscoredNote } from "./lineup-view";

export const dynamic = "force-dynamic";

const VIEWS = [
  ["lineup", "Lineup"],
  ["startsit", "Start/sit"],
  ["waivers", "Waivers"],
] as const;
type View = (typeof VIEWS)[number][0];

function href(p: { league?: number; roster?: number | null; view?: View; ids?: string[] }): string {
  const q = new URLSearchParams();
  if (p.league) q.set("league", String(p.league));
  if (p.roster) q.set("roster", String(p.roster));
  if (p.view) q.set("view", p.view);
  if (p.ids?.length) q.set("ids", p.ids.join(","));
  return `/league?${q}`;
}

function AvailabilityNote({ as_of }: { as_of: AvailabilityAsOf }) {
  const current = as_of.status === "current";
  return (
    <p className="text-sm" aria-label="Availability">
      <Badge variant={current ? "outline" : "destructive"}>availability: {as_of.status}</Badge>{" "}
      {current
        ? `Rosters corrected with ${as_of.applied} Sleeper transaction(s) since the last import.`
        : as_of.note}
      {as_of.missing_weeks.length > 0 && ` Weeks not yet fetched: ${as_of.missing_weeks.join(", ")}.`}
    </p>
  );
}

async function StartSit({
  leagueId,
  rosterId,
  lineup,
  ids,
}: {
  leagueId: number;
  rosterId: number | null;
  lineup: Lineup | null;
  ids: string[];
}) {
  const roster: LineupPlayer[] = lineup
    ? [...lineup.recommended.flatMap((s) => (s.player ? [s.player] : [])), ...lineup.bench].filter(
        (p, i, all) => all.findIndex((q) => q.id === p.id) === i,
      )
    : [];
  // Without a team (a chat source link), show the linked players as they are.
  const picked = (lineup ? ids.filter((id) => roster.some((p) => p.id === id)) : ids).slice(0, 4);
  const result = picked.length >= 2 ? await api.startSit(picked, leagueId) : null;
  const toggle = (id: string) => (picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id]);
  if (lineup && roster.length === 0) return <p className="text-sm text-muted-foreground">This team has no players.</p>;
  return (
    <div className="space-y-3">
      {lineup ? (
        <>
          <p className="text-sm text-muted-foreground">Pick 2–4 of your players to compare next week&apos;s projections.</p>
          <div className="flex flex-wrap gap-2 text-sm" aria-label="Roster players">
            {roster.map((p) => (
              <Link
                key={p.id}
                href={href({ league: leagueId, roster: rosterId, view: "startsit", ids: toggle(p.id) })}
                className={`rounded border px-2 py-1 ${picked.includes(p.id) ? "bg-muted font-semibold" : ""}`}
              >
                {p.name}
              </Link>
            ))}
          </div>
        </>
      ) : (
        !result && <p className="text-sm text-muted-foreground">Pick a team.</p>
      )}
      {result && <StartSitTable result={result} />}
    </div>
  );
}

async function WaiversView({ leagueId }: { leagueId: number }) {
  const w = await api.waivers(leagueId);
  return (
    <div className="space-y-3">
      <AvailabilityNote as_of={w.availability_as_of} />
      <p className="text-sm">
        Week {w.week} · {w.scoring} · rosters synced {new Date(w.rosters_as_of.synced_at).toLocaleString()}
      </p>
      <UnscoredNote items={w.candidates} />
      <Table aria-label="Waivers">
        <TableHeader>
          <TableRow>
            <TableHead>Player</TableHead>
            <TableHead>Pos</TableHead>
            <TableHead className="text-right">Proj</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {w.candidates.map((c) => (
            <TableRow key={c.id}>
              <TableCell>
                {c.name} <span className="text-muted-foreground">{c.team}</span>
                <Status p={c} />
              </TableCell>
              <TableCell>{c.position}</TableCell>
              <TableCell className="text-right tabular-nums">{c.projected_points ?? "–"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export default async function LeaguePage({ searchParams }: PageProps<"/league">) {
  const sp = await searchParams;
  const leagueId = Number(sp.league) || null;
  const view: View = VIEWS.some(([v]) => v === sp.view) ? (sp.view as View) : "lineup";
  const ids = typeof sp.ids === "string" ? sp.ids.split(",").filter(Boolean) : [];
  const leagues = await api.leagues();
  if (leagues.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No leagues imported yet. Run <code>uv run fantasy-rag league import</code>.
      </p>
    );
  }
  const league = leagues.find((l) => l.id === leagueId);
  const rosterId = Number(sp.roster) || (league?.rosters.length === 1 ? league.rosters[0].roster_id : null);
  const lineup = league && rosterId && view !== "waivers" ? await api.lineup(league.id, rosterId) : null;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">My league</h1>
      <div className="flex flex-wrap gap-3 text-sm" aria-label="Leagues">
        {leagues.map((l) => (
          <Link
            key={l.id}
            href={href({ league: l.id, view })}
            className={l.id === leagueId ? "font-semibold underline" : "hover:underline"}
          >
            {l.name ?? `League ${l.id}`} ({l.rosters.length} teams)
          </Link>
        ))}
      </div>
      {league && (
        <>
          <nav className="flex gap-4 border-b text-sm" aria-label="Views">
            {VIEWS.map(([v, label]) => (
              <Link
                key={v}
                href={href({ league: league.id, roster: rosterId, view: v })}
                className={`pb-2 ${v === view ? "border-b-2 border-foreground font-semibold" : "text-muted-foreground"}`}
              >
                {label}
              </Link>
            ))}
          </nav>
          {view !== "waivers" && league.rosters.length > 1 && (
            <div className="flex flex-wrap gap-2 text-sm" aria-label="Teams">
              {league.rosters.map((r) => (
                <Link
                  key={r.roster_id}
                  href={href({ league: league.id, roster: r.roster_id, view })}
                  className={`rounded border px-2 py-1 ${r.roster_id === rosterId ? "bg-muted font-semibold" : ""}`}
                >
                  {r.owner_name ?? `Roster ${r.roster_id}`}
                </Link>
              ))}
            </div>
          )}
        </>
      )}
      {!league ? (
        <p className="text-sm text-muted-foreground">Pick a league.</p>
      ) : view === "waivers" ? (
        <WaiversView leagueId={league.id} />
      ) : view === "startsit" ? (
        <StartSit leagueId={league.id} rosterId={rosterId} lineup={lineup} ids={ids} />
      ) : !lineup ? (
        <p className="text-sm text-muted-foreground">Pick a team.</p>
      ) : (
        <LineupView lineup={lineup} />
      )}
    </div>
  );
}
