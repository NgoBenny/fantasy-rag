// Typed client for the fantasy-rag FastAPI backend (server-side fetches only).
const API_URL = process.env.API_URL ?? "http://localhost:8010";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { cache: "no-store", ...init });
  } catch {
    throw new ApiError(503, `API unreachable at ${API_URL}. Is uvicorn running?`);
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
    const detail = typeof body.detail === "string" ? body.detail : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export type Scoring = "ppr" | "half" | "std";

export interface PlayerRef {
  id: string;
  name: string;
  position: string | null;
  team: string | null;
}

export interface SearchHit extends PlayerRef {
  active: boolean;
}

export interface WeekRow {
  week: number;
  opponent: string | null;
  points: number;
  [stat: string]: number | string | null;
}

export interface PlayerStats {
  player: PlayerRef;
  season: number;
  weeks: [number, number];
  scoring: string;
  games: number;
  total_points: number;
  points_per_game: number | null;
  totals: Record<string, number>;
  by_week: WeekRow[];
  unscored_rules: string[];
}

export interface Availability {
  status: string; // "bye", "Out", "IR", "Questionable", ...
  playable: boolean;
  note?: string;
  source?: string;
  observed_at?: string;
}

export type LineupPlayer = PlayerRef & {
  projected_points: number | null;
  availability: Availability | null;
};

export interface LineupSlot {
  slot: string;
  player: LineupPlayer | null;
  current: LineupPlayer | null;
}

export interface Lineup {
  week: number;
  scoring: string;
  rosters_as_of: { week: number; synced_at: string };
  recommended: LineupSlot[];
  bench: LineupPlayer[];
  changes: { start: string[]; sit: string[] };
  recommended_total: number;
  current_total: number;
  gain: number;
  unhandled_slots: string[];
  no_projection: string[];
  unavailable: ({ name: string } & Availability)[];
  unscored_rules: string[];
  model_version: string;
}

export interface League {
  id: number;
  name: string | null;
  season: number;
  roster_positions: string[];
  rosters: { roster_id: number; owner_name: string | null }[];
}

export const api = {
  health: () => call<{ ok: boolean; season: number; latest_week: number }>("/api/health"),
  freshness: () => call<{ source: string; last_ok: string; age_hours: number }[]>("/api/freshness"),
  trending: () =>
    call<{ players: { player_id: string; full_name: string; position: string; team: string; count: number }[] }>(
      "/api/trending?kind=add&limit=10",
    ),
  search: (q: string, position?: string) =>
    call<SearchHit[]>(`/api/players?${new URLSearchParams({ q, ...(position ? { position } : {}) })}`),
  player: (id: string, scoring: Scoring) =>
    call<PlayerStats>(`/api/players/${encodeURIComponent(id)}?scoring=${scoring}`),
  compare: (ids: string[], scoring: Scoring) =>
    call<{ players: PlayerStats[] }>(`/api/compare?ids=${ids.map(encodeURIComponent).join(",")}&scoring=${scoring}`),
  leagues: () => call<League[]>("/api/leagues"),
  lineup: (leagueId: number, rosterId: number) =>
    call<Lineup>(`/api/leagues/${leagueId}/lineup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roster_id: rosterId }),
    }),
};

export const SHOWN_STATS: [string, string][] = [
  ["targets", "Tgt"],
  ["receptions", "Rec"],
  ["receiving_yards", "Rec Yd"],
  ["receiving_tds", "Rec TD"],
  ["carries", "Car"],
  ["rushing_yards", "Rush Yd"],
  ["rushing_tds", "Rush TD"],
  ["passing_yards", "Pass Yd"],
  ["passing_tds", "Pass TD"],
  ["passing_interceptions", "INT"],
  ["fg_made", "FGM"],
];

export function scoringOf(v: string | string[] | undefined): Scoring {
  return v === "half" || v === "std" ? v : "ppr";
}
