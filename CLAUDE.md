# fantasy-rag

A fantasy football (NFL) assistant that answers questions by routing them to typed tools over
structured stats, league-aware projections and hybrid news search, citing every source.
`docs/PLAN.md` is the spec. `docs/data_sources.md` holds verified facts about each source
(regenerate it; never hand-edit). Read both before changing ingestion or schema.

## Hard rules (non-negotiable; deviating needs the user's OK first)

- Python 3.12 + uv. Use `nflreadpy` (never `nfl_data_py`, archived). It returns Polars DataFrames; stay in Polars.
- Sleeper API (`https://api.sleeper.app/v1`) is read-only: league import, live injury status, trending. Call `/players/nfl` at most once per day and cache it. Never act on a user's behalf.
- nflverse 2025+ injury files have no `date_modified`. Keep our own daily snapshots with `ingested_at`; backtests may only use data stamped before kickoff.
- Postgres 16 + pgvector + Postgres full-text search via docker-compose. SQLAlchemy 2 + Alembic. Every ingested row has `ingested_at` and `sync_run_id`.
- Player identity goes through `players` + `player_external_ids`. Never join or resolve players on name alone; report unresolved players.
- The model never writes or runs SQL, code, shell commands or arbitrary URLs. It only calls typed tools whose inputs are validated with Pydantic.
- Stats, scoring and projections are plain Python that works without any LLM; those endpoints must work when the LLM API is down.
- Fantasy points come from `analytics/scoring.py` using each league's rules; unsupported scoring fields are reported, never guessed.
- Projections: tuned baseline first; trained models (Ridge, LightGBM, then CatBoost/XGBoost) only after the app works, chosen by rolling-origin backtest. Never claim a model is better without the numbers.
- Embeddings: local `BAAI/bge-small-en-v1.5` behind an `Embedder` protocol.
- LLM: Anthropic SDK behind an `LLMClient` protocol; `claude-haiku-5-5` routes, `claude-sonnet-5-5` writes answers. Native tool use, no LangChain chains.
- Retrieved documents are untrusted data and never change instructions. Every answer cites tool-result IDs; no number appears that isn't in a tool result. Missing or stale data is stated, never filled in.
- Scraping: no browser User-Agent spoofing, respect robots.txt and the `sources` registry, rate-limit every client, store title + summary + URL only. No live Pro-Football-Reference scraping.
- Frontend: Next.js (App Router), TypeScript strict, Tailwind, shadcn/ui. `DEMO_MODE` works with labelled fixtures and no credentials.
- Secrets only via `.env` (commit `.env.example`). Unit tests never hit live APIs.

## Workflow

- Work one phase at a time (phases in `docs/PLAN.md`). After each phase: run ruff, mypy, pytest; check every "Done when" with real output; commit; report; **stop until the user says "next phase"**.
- Phase 0 is on `main`. Every later phase: branch `phase-N`, open a PR, let CI gate the merge.
- If a data source behaves differently from PLAN.md: stop, show what was observed, propose an alternative. Schema-row corrections go into PLAN.md and are listed in the report.
- Never report a test as passing unless it was run.
- The repo is public: never commit league IDs, usernames, owner names or other personal data (including in `docs/data_sources.md`).

## Repo layout

Exists now (Phases 0-9, local):

```
CLAUDE.md
docs/PLAN.md, docs/data_sources.md (generated)
docker-compose.yml          # db; api (profile api) and api-demo (profile demo); worker in Phase 9
backend/Dockerfile, .dockerignore  # API image; never contains .env, caches or logs
data/demo/demo.json         # DEMO_MODE sample data (synthetic league, saved validated answers)
.env.example
data/id_overrides.csv       # human-confirmed (provider, external_id) -> GSIS pairs
.github/workflows/ci.yml
backend/
  pyproject.toml, uv.lock, alembic.ini
  alembic/versions/0001_initial.py      # extensions + 13 owned tables
  alembic/versions/0002_structured.py   # nflverse tables, snapshot keys, unresolved_players
  alembic/versions/0003_leagues.py      # league name/settings, weekly matchup results
  alembic/versions/0004_readonly_trgm.py  # fantasy_reader role, trigram name index
  alembic/versions/0006_fantasy_positions.py  # players.fantasy_positions (Sleeper slot eligibility)
  alembic/versions/0005_defense.py      # team_week_defense, player_week_special_teams (from pbp)
  alembic/versions/0007_phase7.py       # league_transactions, league_fetches, query_log columns, fantasy_logger
  alembic/versions/0008_forward_log.py  # forward_runs/inputs, projections (insert-only), model_artifacts
  alembic/versions/0009_calibration.py  # calibration_errors (insert-only), projections.effective_policy
  alembic/versions/0010_calibration_fingerprint.py  # calibration errors keyed by scoring-rules fingerprint
  src/fantasy_rag/config.py             # pydantic-settings, DEMO_MODE
  src/fantasy_rag/cli.py                # `fantasy-rag` entry point
  src/fantasy_rag/db.py                 # engine; read-only engine for tools (SET ROLE fantasy_reader)
  src/fantasy_rag/verify_sources.py     # live source check -> docs/data_sources.md
  src/fantasy_rag/ingest/base.py        # sync_run, COPY-based upsert, unresolved report
  src/fantasy_rag/ingest/nflverse.py    # pure frame normalizers + ID-only resolve()
  src/fantasy_rag/ingest/sleeper.py     # Sleeper client, injury/trending frames
  src/fantasy_rag/ingest/structured.py  # `ingest structured` orchestrator + sources registry
  src/fantasy_rag/ingest/league.py      # Sleeper league import (settings, rosters, matchups)
  src/fantasy_rag/ingest/news.py        # news feeds (ESPN JSON, CBS/Yahoo/PFT/32 team RSS) -> stories
  src/fantasy_rag/ingest/entity_link.py # tag stories/chunks with player + team IDs
  src/fantasy_rag/ingest/news_job.py    # `ingest news` + `ingest news-report` (coverage check)
  src/fantasy_rag/index/chunker.py, embedder.py  # ~300-word chunks; Embedder protocol + local bge-small
  src/fantasy_rag/retrieval/hybrid.py   # vector + full-text, RRF, player/recency filters, dedupe
  src/fantasy_rag/retrieval/rerank.py, evaluate.py  # optional bge-reranker; `eval news`
  src/fantasy_rag/ingest/pbp.py         # play-by-play -> team DEF + special-teams stats (pure)
  src/fantasy_rag/analytics/scoring.py  # Sleeper scoring keys (incl. team DEF) -> points
  src/fantasy_rag/analytics/scoring_check.py  # our points vs Sleeper's players_points
  src/fantasy_rag/analytics/queries.py  # parameterized queries, name resolution, scoring presets
  src/fantasy_rag/ml/baseline.py        # EWM-of-league-points baseline (tuned half-life)
  src/fantasy_rag/ml/backtest.py, tune.py  # rolling-origin backtest, `fantasy-rag backtest`
  src/fantasy_rag/ml/features.py        # point-in-time features at each week's Tuesday 12:00 ET cutoff
  src/fantasy_rag/ml/train.py, ranges.py  # Ridge/LightGBM per position+component; conformal ranges
  src/fantasy_rag/ml/benchmark.py       # `fantasy-rag benchmark`: frozen comparison (docs/phase8_plan.md)
  src/fantasy_rag/ml/predict.py         # `fantasy-rag predict`: Tuesday forward run (insert-only log)
  src/fantasy_rag/ml/serve.py           # the one projection source for tools/API (ACTIVE policy per position)
  src/fantasy_rag/ml/forward_results.py # forward results by scoring rules; Tuesday statuses; forward-check
  src/fantasy_rag/ingest/health.py, retry.py  # source health (fresh/stale/failing); transient-error retries
  src/fantasy_rag/jobs/locks.py, backup.py    # fixed-ID advisory locks; verified backups + restore drill
README.md, docs/architecture.md, docs/demo.gif  # overview, data flow, demo recording (DEMO_MODE)
scripts/notify.ps1, scripts/make_gif.py     # Windows toast on job failure; demo GIF assembly
scripts/setup_runner.ps1    # the separate checkout of reviewed main that scheduled tasks run from
  src/fantasy_rag/agent/tools.py        # 10 typed tools + run_tool (Pydantic-validated, no SQL)
  src/fantasy_rag/agent/router.py       # haiku picks tools (<= 4 rounds) -> sonnet writes the answer
  src/fantasy_rag/agent/llm.py, prompts.py  # LLMClient protocol + Anthropic client; static prompts
  src/fantasy_rag/agent/answer.py       # answer contract; unsupported-number / citation checks
  src/fantasy_rag/agent/evaluate.py     # `eval answers`: 60-question golden set + no-support cases
  src/fantasy_rag/agent/grounding.py    # optional paid audit of saved answers; never runs in chat
  src/fantasy_rag/analytics/optimizer.py  # lineup optimizer (exact assignment, locks, empty slots)
  src/fantasy_rag/api/main.py           # FastAPI HTTP layer (port 8010); every route also serves DEMO_MODE
  src/fantasy_rag/api/chat.py           # POST /api/chat: SSE over the Phase 6 agent, insert-only logging
  src/fantasy_rag/api/demo.py           # DEMO_MODE store + chat replay from data/demo/demo.json
  src/fantasy_rag/agent/budget.py       # spending guard: counted-token reservations, overrun lockout
  src/fantasy_rag/analytics/availability.py  # roster snapshot + Sleeper transactions -> availability
  src/fantasy_rag/ingest/transactions.py  # `league sync-transactions` (hourly)
  src/fantasy_rag/demo_record.py        # `demo record`: builds demo.json (free; reuses saved answers)
  tests/eval/golden.yaml                # golden stats questions (20)
  tests/eval/golden_news.yaml           # 20 labelled news questions (+15 bare-name queries)
  tests/eval/golden_answers.yaml        # 15 mixed + 5 out-of-scope questions on the user's rosters
  tests/eval/golden_fresh.yaml          # held-out cases, scored apart from the frozen 60
  tests/unit/, tests/integration/, tests/fixtures/phase1/ (recorded slices; record_phase1.py)
frontend/                   # Next.js 16 App Router + Tailwind + shadcn/ui; pages: /, players, compare, chat, league, model; e2e/ Playwright (live, demo, paid configs)
```

Deferred: hosted deployment (notes in docs/PLAN.md, Phase 9).

## Conventions

- src layout; run everything through `uv run` from `backend/`.
- ruff: line length 100, rules E, F, I, B, UP, SIM; `ruff format`. mypy `--strict` on `src/` (pydantic plugin; `nflreadpy` has no type info, so it is ignored).
- Timestamps are `timestamptz` in UTC. nflverse `gameday` + `gametime` are US/Eastern local strings: convert with an explicit `America/New_York` zone.
- Migrations are hand-written Alembic files, one per phase. No FK from an owned table to an nflverse-mirroring table.
- Tools: inputs are Pydantic models in `agent/tools.py`; tools read through `get_readonly_engine()`. Player names resolve by trigram word similarity over fantasy positions; ties prefer players with stats this season; anything still ambiguous raises `AmbiguousPlayer` with candidates (never a guess). League rules are rebuilt from raw `scoring_settings` on every call.
- Frontend: server components fetch the API (`src/lib/api.ts`); page state lives in the URL (search, compare picks, league/roster), so pages need no client JS (exceptions: chat, and the start/sit picker, which builds each click on its pending selection so quick clicks never drop a pick). `cacheComponents` is off: every page shows live data. Next.js 16 differs from older versions; read `frontend/AGENTS.md` first.
- News: only title + summary (<= 500 chars) + link are stored, capped by `sources.storage_allowed`; feeds' full article text is ignored. Player tags come from ESPN athlete IDs, team-site editorial tags, and exact full-name mentions of current fantasy players; a full name shared by several players needs the story's team to decide, else it's reported as ambiguous. Embeddings: `BAAI/bge-small-en-v1.5` via fastembed (ONNX, CPU), cached in data/cache/fastembed; tests use a fake embedder.
- Shared dev DB: sessions in other worktrees use the same Postgres. Coordinate migration numbers before applying, and never downgrade the dev DB.
- Scoring: `ScoringRules.from_sleeper` keeps only non-zero keys (Sleeper ships unused keys at 0). Keys without a `DERIVE` entry are reported as unsupported; team DEF and special-teams `st_ff`/`st_fum_rec` need play-by-play and are not scored yet.
- Snapshots can't be backfilled: the scheduled task (scripts/register_daily_ingest.ps1) must stay registered, and the PC on with Docker available, until ingestion moves to a hosted worker (Phase 9).
- Ingestion: every write goes through `ingest.base.upsert` (re-runs over identical data change 0 rows) inside `sync_run`. Players resolve by ID only (`nflverse.resolve`); unmatched rows go to `unresolved_players`. Name-based `candidates` are hints for a human, never applied.
- httpx clients: User-Agent `fantasy-rag/0.1 (+https://github.com/NgoBenny/fantasy-rag)`, a timeout, and a rate limit.
- pytest markers: `live` (network; deselected by default), `llm` (calls the paid Claude API; deselected by default) and `db` (needs Postgres; uses a throwaway `<db>_migtest` database, never the dev DB).
- Chat (`api/chat.py`): events are progress* -> exactly one of answer | withheld | error -> done. Progress text comes only from fixed templates and resolved tool output (`router.tool_progress`), never model text; the validated answer and its sources arrive together in one event; a rejected draft never leaves the server (withheld answers carry a fixed message). The agent runs in a bounded thread pool that owns its connections; a client disconnect cancels before the next model or tool call. Each request writes one `query_log` row through `get_log_engine()` (role `fantasy_logger`: INSERT on query_log only; read the id with currval, not RETURNING).
- Spending: `AnthropicLLM(budget=Budget(cap))` counts each request (`messages.count_tokens`), reserves input + max_tokens before calling, runs with SDK retries off, and locks on an overrun. A strong guard on our spending, not a guarantee about the provider's bill. `LLM_BUDGET_USD` caps an API process; paid tests run their own capped process.
- Waiver availability (`analytics/availability.py`) is conditional on assumptions Sleeper doesn't document (listed in the module): roster snapshot + completed transactions replayed from 15 min before the snapshot's fetch, atomic per transaction; status `current` only when every week since the snapshot was fetched successfully within 2 h, else `stale` or `uncertain` (also `uncertain` when a replayed move involves a Sleeper ID we can't map, team DEFs excepted). `raw` transaction records hold Sleeper user IDs: never export them.
- Projections (Phase 8): every caller goes through `ml/serve.project`. `serve.ACTIVE` names the policy per position (empty = baseline everywhere; changing it is a reviewed code change). A non-baseline policy is read from the week's latest forward run when that run used the same scoring rules, else the baseline is served. Each projection carries `range_80` from its own policy's recorded errors in `calibration_errors` under the same scoring-rules fingerprint (frozen seed + finalized forward weeks; never rebuilt). A challenger's points are the baseline plus the change in the components it predicts, so league rules it doesn't model count as in the baseline; a challenger row with too little history has `effective_policy = 'baseline'` and is served as the baseline. Forward-log tables are insert-only (a trigger rejects UPDATE/DELETE); `predict` reads inside one REPEATABLE READ transaction and records the snapshot's database clock time; a run is `on_time` only if that snapshot precedes the Tuesday 12:00 ET deadline. The evaluation rules in `docs/phase8_plan.md` are frozen.
- Demo mode (`DEMO_MODE=true`): no engine, LLM client or .env is used; `FANTASY_RAG_ENV_FILE` can point settings at another (or missing) env file.
- Agent: the router sees tool schemas only; tool results reach the writer as JSON inside `<result id=...>` tags with `<` escaped, so retrieved text can't close the wrapper. Answers are checked in plain Python (`agent/answer.py`): every number must appear in a tool result, the question or the context; every citation must be a result ID from that turn.
- Local Postgres listens on 5433 when another Postgres holds 5432 (set `DB_PORT` and `DATABASE_URL` in `.env`).
- Everything binds to 127.0.0.1 (compose ports, `next dev/start --hostname 127.0.0.1`, uvicorn's default). Use `127.0.0.1`, not `localhost`, in `DATABASE_URL`: on this PC `localhost` tries IPv6 first and stalls. Engines use a 10 s connect timeout.
- Jobs (Phase 9): every CLI job takes one advisory lock in `cli.main` (`jobs/locks.py`: STRUCTURED for ingest/league import/predict/calibration-seed, NEWS, TRANSACTIONS, BACKUP); nothing underneath locks. A second copy exits "already running". `predict` writes nothing if an on-time run for the week exists. Source health comes from `sync_runs` (`failing` = last 2 attempts failed); `FANTASY_RAG_FAIL_SOURCE=<source>` injects an outage for drills only. Failed scheduled jobs show a Windows toast; logs are in `data/logs/`.
- Scheduled tasks run from a separate checkout of reviewed `main` (`scripts/setup_runner.ps1`, default `%USERPROFILE%antasy-rag-runner`) with its own ignored `.env`, `.venv`, `data/cache` and `data/logs`; it shares the database container (compose `name: fantasy-rag`). Scheduled runs never pull: after a merge, run `setup_runner.ps1` between jobs. Register tasks only from the runner (`-Register`); registering from a dev tree points the tasks back at code being edited. Both checkouts share one database, so schema changes still need coordination.

## Commands

PowerShell 5.1 has no `&&`: run each command separately.

```
# repo root
docker compose up -d db                # Postgres 16 + pgvector
docker compose ps                      # expect db "healthy"

# backend/
uv sync                                # install from uv.lock
uv run ruff check .                    # lint
uv run ruff format --check .           # format check (drop --check to fix)
uv run mypy                            # type-check src/
uv run pytest                          # unit + db tests; live tests deselected
uv run pytest -m live                  # live checks: top-10 PPR, 20 stats questions, news retrieval (dev DB)
uv run alembic upgrade head            # apply migrations
uv run alembic downgrade base          # drop everything
uv run fantasy-rag verify-sources      # sample every core source -> docs/data_sources.md
uv run fantasy-rag ingest structured   # nflverse 2022+ and Sleeper -> Postgres (idempotent)
uv run fantasy-rag ingest news         # news feeds -> documents, chunks, embeddings (idempotent)
uv run fantasy-rag ingest news-report  # stories per source + % of rostered-player mentions tagged
uv run python tests/fixtures/record_phase1.py   # re-record test fixtures (network)
uv run fantasy-rag league import       # your Sleeper leagues (SLEEPER_USERNAME or --league-id)
uv run fantasy-rag league check-scoring  # our points vs Sleeper's, per player-week (exit 1 on any miss)
uv run fantasy-rag tool get_player_stats '{"player":{"name":"Amon-Ra"},"last_n":3}'  # any tool by hand
uv run fantasy-rag backtest             # tune baseline on 2024, report 2025; writes model_runs
uv run fantasy-rag benchmark            # Phase 8 frozen comparison (~25 min); saves predictions in data/logs/
uv run fantasy-rag benchmark --predictions ../data/logs/<file>.pkl   # re-report without retraining
uv run fantasy-rag predict              # forward run for the next week (the Tuesday task runs it)
uv run fantasy-rag calibration-seed     # once: frozen range calibration (refused if already recorded)
uv run fantasy-rag forward-check        # exit 1 if this week's on-time forward run is missing near/after the deadline
uv run fantasy-rag backup               # verified dump + roles + manifest to %USERPROFILE%antasy-rag-backups (BACKUP_DIR)
uv run fantasy-rag backup --verify-latest   # restore newest dump into a fresh container; compare with its manifest
uv run fantasy-rag eval news           # recall@5 / recall@1 / MRR per search mode (--rerank: 1 GB model)
uv run fantasy-rag ask "bucky irving or jaylen warren?" --league-id 1   # one cited answer (Claude API, ~1-3 cents)
uv run fantasy-rag eval answers        # golden set through router + writer (~$1-2); --only mixed,a05; report in data/logs/
uv run fantasy-rag eval answers --rescore ../data/logs/<report>.json   # re-grade a saved run under the current rubric (no API calls)
uv run fantasy-rag eval grounding --report ../data/logs/<report>.json --only a09,a15   # advisory paid review of saved answers; never certifies grounding
uv run pytest -m llm                   # Phase 6 checks: golden targets + prompt-injection fixture (Claude API; set LLM_TEST_BUDGET_USD)
uv run fantasy-rag league sync-transactions   # Sleeper adds/drops/trades -> waiver availability (hourly task runs it)
uv run fantasy-rag demo record --report ../data/logs/<eval report>.json   # rebuild data/demo/demo.json (free)
uv run fantasy-rag demo record --report ... --record-question "..." --budget-usd 0.10   # add one live answer (paid, capped; kept in data/demo/recorded_chats.json)

uv run uvicorn fantasy_rag.api.main:app --port 8010 --reload   # API (port 8000 is taken on this PC); --reload so Playwright never reuses stale code

# frontend/
npm run dev -- --port 3010             # web app on 127.0.0.1; reads API_URL (default http://localhost:8010)
npm run lint                           # eslint
npm test                               # Vitest unit tests
npm run build                          # type-check + production build (no API needed)
npx playwright test                    # live browser tests, unpaid: own API (key blanked) on 8012 + proxy + web on 3010; stop your dev server first
npx playwright test -c playwright.demo.config.ts   # demo mode, no DB/key/.env (CI runs this)
LIVE_LLM=1 npx playwright test -c playwright.paid.config.ts   # one real chat question, API capped at $0.10 (opt-in, paid)

# repo root: Docker
docker compose --profile api up -d db api      # API in a container on 8010 (reads .env)
docker compose --profile demo up api-demo      # demo API: sample data, no .env, database or key

# repo root: daily snapshot collection (Windows Task Scheduler, current user)
powershell -File scripts/setup_runner.ps1           # create/update the runner checkout (reviewed main); refuses while a task runs
powershell -File scripts/setup_runner.ps1 -Register # same, then (re-)register every task to run from it
powershell -File scripts/register_daily_ingest.ps1   # registers tasks for the checkout it's run from; daily 6pm + Wed-Sat 2pm/9pm; logs in that checkout's data/logs/
powershell -File scripts/ingest_daily.ps1            # one run by hand, same as the task
powershell -File scripts/ingest_daily.ps1 -Job news  # one news run (hourly task "fantasy-rag news"); refreshes data/logs/news_report.json
powershell -File scripts/ingest_daily.ps1 -Job forward  # ingest + forward run (task "fantasy-rag forward", Tuesdays 10:30 ET)
powershell -File scripts/ingest_daily.ps1 -Job forward-check  # toast if the on-time run is missing (task, Tuesdays 11:30 ET)
powershell -File scripts/ingest_daily.ps1 -Job backup  # daily backup; Sundays also the restore drill (task "fantasy-rag backup", 7 PM)
RECORD_GIF=1 npx playwright test -c playwright.demo.config.ts gif.demo   # (frontend/) frames, then: uv run python ../scripts/make_gif.py
```
