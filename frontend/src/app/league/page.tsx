import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  type AvailabilityAsOf,
  type Lineup,
  type LineupPlayer,
  WAIVER_POSITIONS,
  type WaiverPosition,
  api,
} from "@/lib/api";
import { UsageAsOfLine, UsageContext, UsageNote } from "@/components/usage";
import { rostersLabel } from "@/lib/lineup";

import { LineupView, Range, StartSitTable, Status, UnscoredNote } from "./lineup-view";
import { RosterPicker } from "./roster-picker";

export const dynamic = "force-dynamic";

const VIEWS = [
  ["lineup", "Lineup"],
  ["startsit", "Start/sit"],
  ["waivers", "Waivers"],
] as const;
type View = (typeof VIEWS)[number][0];

function href(p: {
  league?: number;
  roster?: number | null;
  view?: View;
  ids?: string[];
  pos?: WaiverPosition | null;
}): string {
  const q = new URLSearchParams();
  if (p.league) q.set("league", String(p.league));
  if (p.roster) q.set("roster", String(p.roster));
  if (p.view) q.set("view", p.view);
  if (p.ids?.length) q.set("ids", p.ids.join(","));
  if (p.pos) q.set("pos", p.pos);
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
  if (lineup && roster.length === 0) return <p className="text-sm text-muted-foreground">This team has no players.</p>;
  return (
    <div className="space-y-3">
      {lineup ? (
        <>
          <p className="text-sm text-muted-foreground">Pick 2–4 of your players to compare this week&apos;s projections.</p>
          <RosterPicker
            players={roster}
            picked={picked}
            base={href({ league: leagueId, roster: rosterId, view: "startsit" })}
          />
        </>
      ) : (
        !result && <p className="text-sm text-muted-foreground">Pick a team.</p>
      )}
      {result && <StartSitTable result={result} />}
    </div>
  );
}

async function WaiversView({ leagueId, rosterId, pos }: { leagueId: number; rosterId: number | null; pos: WaiverPosition | null }) {
  const w = await api.waivers(leagueId, pos ?? undefined);
  const tabs: [WaiverPosition | null, string][] = [[null, "All"], ...WAIVER_POSITIONS.map((p) => [p, p] as [WaiverPosition, string])];
  return (
    <div className="space-y-3">
      <nav className="flex gap-2 text-sm" aria-label="Positions">
        {tabs.map(([p, label]) => (
          <Link
            key={label}
            href={href({ league: leagueId, roster: rosterId, view: "waivers", pos: p })}
            aria-current={p === pos ? "page" : undefined}
            className={`rounded border px-2 py-1 ${p === pos ? "bg-muted font-semibold" : ""}`}
          >
            {label}
          </Link>
        ))}
      </nav>
      <p className="text-sm font-medium" aria-label="Waiver list">
        {pos
          ? `Unrostered ${pos === "K" ? "kickers" : `${pos}s`} by projected points`
          : "Highest projected points across positions (not ranked for your roster)"}
      </p>
      <AvailabilityNote as_of={w.availability_as_of} />
      <p className="text-sm">
        Week {w.week} · {w.scoring} · {rostersLabel(w.rosters_as_of)}
      </p>
      <UnscoredNote items={w.candidates} />
      {w.usage_as_of && <UsageAsOfLine usage={{ window: null, facts: [], as_of: w.usage_as_of }} />}
      <Table aria-label="Waivers">
        <TableHeader>
          <TableRow>
            <TableHead>Player</TableHead>
            <TableHead>Pos</TableHead>
            <TableHead className="text-right">Proj</TableHead>
            <TableHead className="text-right">80% range</TableHead>
            <TableHead>Usage context (last 3 games)</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {w.candidates.length === 0 && (
            <TableRow>
              <TableCell colSpan={5} className="text-muted-foreground">
                No unrostered {pos ?? "players"} with a projection this week.
              </TableCell>
            </TableRow>
          )}
          {w.candidates.map((c) => (
            <TableRow key={c.id}>
              <TableCell>
                {c.name} <span className="text-muted-foreground">{c.team}</span>
                <Status p={c} />
              </TableCell>
              <TableCell>{c.position}</TableCell>
              <TableCell className="text-right tabular-nums">{c.projected_points ?? "–"}</TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                <Range r={c.range_80} />
              </TableCell>
              <TableCell className="max-w-sm whitespace-normal">
                <UsageContext usage={c.usage} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-xs text-muted-foreground">Ranked by projection; usage is shown as context only.</p>
      <UsageNote />
    </div>
  );
}

export default async function LeaguePage({ searchParams }: PageProps<"/league">) {
  const sp = await searchParams;
  const leagueId = Number(sp.league) || null;
  const view: View = VIEWS.some(([v]) => v === sp.view) ? (sp.view as View) : "lineup";
  const ids = typeof sp.ids === "string" ? sp.ids.split(",").filter(Boolean) : [];
  const pos = WAIVER_POSITIONS.find((p) => p === sp.pos) ?? null;
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
        <WaiversView leagueId={league.id} rosterId={rosterId} pos={pos} />
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
