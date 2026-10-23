# Architecture

fantasy-rag answers fantasy football questions by routing them to typed tools over
structured data, a league-aware scoring engine, projections and hybrid news search. Every
number in an answer must come from a tool result, and every claim cites one. It runs
locally: Postgres in Docker, a FastAPI backend, a Next.js frontend, and Windows scheduled
tasks for collection. Everything listens on 127.0.0.1 only.

```mermaid
flowchart LR
  subgraph Sources
    NV[nflverse<br/>stats, schedules, snaps, injuries, pbp]
    SL[Sleeper API<br/>status, trending, leagues, transactions]
    NW[News feeds<br/>ESPN JSON, CBS/Yahoo/PFT, 32 team RSS]
  end
  subgraph Jobs["Scheduled jobs (Windows Task Scheduler, advisory locks)"]
    ING[ingest structured + league import<br/>daily, Wed–Sat 3x]
    NEWS[ingest news + transactions<br/>hourly]
    PRED[predict<br/>Tue 10:30 ET]
    CHK[forward-check<br/>Tue 11:30 ET]
    BK[backup + restore drill<br/>daily / Sundays]
  end
  DB[(Postgres 16<br/>pgvector + full-text)]
  subgraph API["FastAPI (127.0.0.1:8010)"]
    TOOLS[typed tools<br/>read-only role]
    SERVE[ml/serve<br/>baseline unless ACTIVE]
    CHAT[router: haiku<br/>writer: sonnet<br/>Python answer checks]
  end
  WEB[Next.js (127.0.0.1:3010)<br/>chat, players, compare, league, model]
  NV --> ING --> DB
  SL --> ING
  SL --> NEWS
  NW --> NEWS --> DB
  DB --> PRED --> DB
  DB --> TOOLS --> CHAT
  SERVE --> TOOLS
  DB --> SERVE
  CHAT -- SSE: progress, then one validated answer with sources --> WEB
  TOOLS --> WEB
  CHK -. toast if missing .-> WEB
```

## Data flow

1. **Ingestion** writes every row with `ingested_at` and `sync_run_id`.
   - Each source has its own `sync_runs` row.
   - One failing source never blocks the others, and transient HTTP errors are retried (3 tries, backoff).
   - Players resolve by ID only; anything unresolved is reported, never guessed.
2. **Source health** (`ingest/health.py`) reads `sync_runs`. Each source is `fresh`, `stale` (last success too old) or `failing` (its last 2 attempts failed). The page header shows every source that isn't fresh.
3. **Tools** (`agent/tools.py`) are typed, Pydantic-validated functions over a read-only database role. The model never writes SQL.
4. **Chat** (`api/chat.py`):
   - the router model picks tools;
   - the writer model drafts a structured answer;
   - plain Python checks every number and citation, allowing one repair, otherwise the answer is withheld.
   - The browser receives progress lines written by application code, then one validated answer with its sources.
5. **Projections** come from one serving function (`ml/serve.py`):
   - The baseline (a tuned weighted average) serves every position.
   - A trained policy serves a position only after a reviewed code change.
   - Every projection carries an 80% range calibrated on its own policy's recorded errors.

### In-progress weeks and lineup locks

Live tools, the header and chat use the same schedule-based projection week. A partial
Thursday stat import cannot advance it. A week remains active until four hours after
its final scheduled kickoff (the same completion estimate used by usage); unknown kickoff
times keep the week open. If no schedule is loaded, the fallback is the latest stats week
plus one, capped at week 18. Historical stats and the immutable Tuesday forward log are unchanged.

At a player's scheduled kickoff, the lineup optimizer fixes a current starter in that
exact slot and excludes started bench players from new assignments. Those locks also
preserve starters with no projection, a negative projection or an Out designation. The
page labels locked slots and started bench players. Totals remain projections, not live
scores. Locks depend on the imported roster and schedule, so source freshness still matters.

## Forward log (Phase 8)

Every Tuesday at 10:30 ET, `predict` predicts the coming week with both the baseline and TE Ridge.
- It reads one database snapshot and saves the inputs, model hash, scoring rules and predictions in insert-only tables (`forward_runs`, `forward_inputs`, `projections`, `model_artifacts`).
- It is labelled `on_time` only if its snapshot precedes the Tuesday 12:00 ET deadline.
- Once a week is final (4 days after its last game), its first on-time run is scored once into `calibration_errors`, keyed by a fingerprint of the scoring rules.
- The Model page compares baseline and Ridge on matched player-weeks of those unseen weeks.
- `forward-check` raises a Windows toast if Tuesday's run is missing 30 minutes before the deadline.

## Job ownership

Each CLI command takes one Postgres advisory lock (`jobs/locks.py`):
- `STRUCTURED` for ingest, league import and predict, which share it so they never overlap;
- `NEWS`, `TRANSACTIONS` and `BACKUP` for the others.

The lock lives on its own connection and is released explicitly in `finally`. A second copy of a job exits with "already running".

## Backups

`fantasy-rag backup` dumps the database from an exported snapshot and, in that same snapshot, writes a manifest (row counts and checksums). It also saves the roles, which `pg_dump` omits. Dumps go to `%USERPROFILE%\fantasy-rag-backups`, outside the repo.

`backup --verify-latest` restores the newest dump into a fresh, separate Postgres container (roles first) and compares it with its manifest:
- counts and checksums;
- the read-only and insert-only role grants;
- the insert-only triggers;
- the extensions.

## Not hosted (yet)

Hosted deployment (Neon, Render, Vercel, GitHub sign-in) is deferred; see `docs/PLAN.md`. Collection depends on this PC being on: a missed Tuesday is reported as missing on the Model page, never filled in.
