# Phase 8 plan: trained projections (frozen before training)

Written Oct 21, 2026, after the Step 0 data checks and before any model was trained.
The evaluation rules below are fixed now; changing one later needs a dated note saying
why, and results under the original rule are still reported.

**Outcome either way:** this phase ships the evaluation, prediction ranges where they
validate, and the Model page. The baseline stays the production model for any position
where no challenger wins.

## Step 0 findings (Oct 21, 2026)

| Check | Observed | Consequence |
|---|---|---|
| Stats history | 2022–2025 complete (~6,400 skill player-weeks a season); 2026 through week 6 | Enough to train per position |
| Snap counts | 99.7–100% of QB/RB/WR/TE stat rows have a snap row, 2022–2026 | Snap share is usable |
| Betting lines | `spread_line`/`total_line` present for every played game; nflverse documents them as **closing** lines | Not known at a Tuesday cutoff: excluded from v1 |
| Injury snapshots | Our own snapshots start **Oct 20, 2026** (one day) | No point-in-time injury history: no injury features in v1 |
| Publication timing | nflverse: stats rebuilt nightly after each game day; snap counts at 0/6/12/18 UTC (depends on PFR); NFL stat corrections Mon–Wed. Our own sync history starts Oct 20, 2026 | Tuesday-noon availability of week w-1 data is an **assumption** (A1, A2 below), not something we can verify for 2022–2025 |
| Stored stats | We hold final (corrected) stats; the Tuesday-noon version isn't kept anywhere | Historical features use slightly better data than a Tuesday user had (A3) |
| FantasyPros | `load_ff_rankings("all")` has weekly ECR pages for 2022–2026 (QB, PPR RB/WR/TE, K), scraped **Fridays**; 4,746 FantasyPros IDs map to our players | Comparison is possible but gives FantasyPros 3 extra days of news; reported with that caveat. Ranks only, so no MAE |
| League bonus rules | None of the 4 leagues score yardage or TD bonuses. Threshold keys exist only for K (FG distance and misses) and team DEF (points/yards allowed) | Both positions stay on the baseline / unscored path in v1; any `bonus_*` or threshold key on a modeled position is reported in `unscored_rules`, never scored from a point estimate |
| Existing backtest | Picks start/sit pairs from each model's own top N | Replaced by one model-free pair set (below) |
| `projections` table | Exists, empty; no cutoff or scoring-rules columns | Migration 0008 extends it into the immutable forward log |

## Prediction time and availability

- **Cutoff:** Tuesday 12:00 America/New_York of week w (the week being predicted).
  Each feature value must be available by then.
- **Availability rule (in code, tested):** a game's stats and snap counts are usable
  only if the game kicked off at least 12 hours before the cutoff. A Tuesday or
  later make-up game of week w-1 is therefore excluded until the next cutoff.
- **Assumptions, stated wherever results are shown:**
  - A1. nflverse player stats for games kicking off by Monday evening were published
    before Tuesday noon (documented as nightly; not verifiable for past seasons).
  - A2. PFR snap counts for those games were published before Tuesday noon.
  - A3. We use corrected stats; the version a user saw on Tuesday may differ slightly.
- **Who is evaluated:** QB/RB/WR/TE player-weeks with a stat row (the player played)
  and at least one usable earlier game, for every model alike. Whether a player plays
  isn't predicted. No historical injury status is applied to anyone; today's status is
  never applied to past games. This is "points if he plays", the same quantity start/sit
  compares. It uses one post-cutoff fact (that he played), equally for every model; this is
  disclosed.

## Features (v1), all computed per cutoff from games passing the availability rule

- Per stat component: exponentially weighted and last-3 means of the player's past games
  (targets, receptions, receiving yards and TDs, carries, rushing yards and TDs, pass
  attempts, passing yards, TDs and interceptions, fumbles lost).
- Usage: snap share (offense %), target share, carry share; games played this season.
- Prior-season averages (blended in early weeks, as the baseline does).
- Opponent: fantasy points (PPR) the opponent allowed to the position over its past games,
  and home/away (from the schedule, known in advance).
- Excluded in v1: betting lines (closing only), injury flags (no history), depth charts
  (2023–2024 weekly charts have no publish time).

## Models

- Positions QB, RB, WR, TE. K and team DEF keep the baseline.
- One model per position and stat component; points come from `analytics/scoring.py`
  under each scoring format or league, so leagues keep their own rules.
- Ridge (scikit-learn) and LightGBM. CatBoost and XGBoost stay deferred until Ridge or
  LightGBM wins somewhere.
- Every learned step (imputation, scaling, model fit) is fit only on rows whose target
  game is before the cutoff. Hyperparameters: a small fixed grid, chosen on 2023 only.

## Windows (rolling origin, retrained every week)

| Window | Use |
|---|---|
| 2022 | Training history only |
| 2023 | Hyperparameter choice (both models), training thereafter |
| 2024 | Out-of-sample predictions whose errors seed range calibration |
| 2025 | Historical benchmark: the promotion decision. The baseline's 2025 numbers were already seen in Phase 4, so this is benchmarking, not an untouched test |
| 2026 from week 8 | Forward evaluation: predictions stored at each Tuesday cutoff before the games, never edited |

For target week w, a model trains on all rows with target week before w (from 2022 on)
and predicts week w. Week 7 of 2026 is not backfilled: its cutoff (Oct 20) has passed.

## Metrics (same players, weeks and pairs for every model)

- **MAE** of fantasy points, by position and overall, in PPR, half and standard.
- **Spearman** within each (week, position) over the comparison set, averaged over weeks.
- **Start/sit accuracy** on one fixed pair set: each (week, position), the top N players
  (QB 24, RB 48, WR 60, TE 24) by points per game over their usable earlier games, a
  model-free choice known at the cutoff; all same-position pairs among them whose actual
  points differ.
- **Ranges:** 80% coverage and mean width, by position and scoring format.
- Every table shows sample counts.
- **Uncertainty:** 95% intervals for each model-minus-baseline difference from a
  week-block bootstrap (1,000 resamples of weeks), fixed seed.
- **FantasyPros (PPR, ranking metrics only):** Spearman and start/sit accuracy on the
  same pair set, restricted to players FantasyPros ranked, with the Friday caveat.

## Promotion rule (frozen)

Decided per position, on the 2025 benchmark in PPR:

1. MAE at least 2% lower than the baseline, with the 95% interval of the difference
   entirely below zero; and
2. Spearman higher than the baseline (point estimate); and
3. Start/sit accuracy higher than the baseline (point estimate).

If both challengers qualify, the one with the lower MAE wins. Half and standard results
are reported; if a winner is worse than the baseline on MAE in either of those formats,
that is flagged and the position stays on the baseline. Forward 2026 results are
reported weekly but don't decide promotion in this phase (too few weeks); they can
demote a model later.

**Fallbacks:** the baseline serves any player with fewer than 3 usable earlier games, any
position without a promoted model, K and team DEF, and any request where the promoted
model has no prediction.

## Prediction ranges (split conformal on final points)

- Residual = actual minus predicted fantasy points (not per component), per position and
  scoring format, from out-of-sample predictions only.
- For target week w, the range uses residuals from every earlier out-of-sample week (2024
  on, then growing). They come from models trained before those weeks' outcomes, so no
  residual comes from a model that saw its own outcome. When the model is retrained, the
  residual pool keeps the errors as they were made; it isn't recomputed.
- Bounds: predicted + the finite-sample quantiles at levels ceil((n+1)·0.1)/n and
  ceil((n+1)·0.9)/n of the residuals (in numpy, no new package).
- Fewer than 50 residuals: no range is shown, and "not enough history" is stated.
- A range ships for a (position, format) only if 2025 coverage lands in 75–85%. 80% is a
  measured target, not a promise.
- Tested: the quantile function on small known samples, and the under-50 fallback.

## Leakage tests

1. **Perturbation:** change every stat, snap count and schedule result after a cutoff;
   features, predictions and ranges for that cutoff must be identical.
2. **Availability:** each feature row records the latest game it used; that game's
   kickoff must be 12 hours or more before the cutoff.
3. **Fitting:** training, scaling, imputation and calibration rows all have target weeks
   before the predicted week (asserted inside the backtest loop).
4. These prove the code uses only data available under A1–A3; they can't prove what was
   actually published by Tuesday in past seasons. Reports say that.

## Forward log

- Migration 0008 turns the empty `projections` table into an immutable log: cutoff
  time, model version, scoring key (preset or league ID) with the raw scoring settings
  used, player, predicted components, points, p10/p90, created time.
- Primary key (cutoff, model version, scoring key, player). Rows are only inserted (a
  re-run keeps the original). A trigger rejects UPDATE and DELETE.
- `fantasy-rag predict` writes them. Run by hand (or the scheduled task) after each
  Tuesday cutoff, before Thursday's game.
- Scored against actual results once the games are played.

## Deliverables

- `ml/features.py`, `ml/train.py`, `ml/predict.py`; the backtest extended to the frozen
  comparison; `model_runs` rows for each model.
- Model page: baseline vs Ridge vs LightGBM by position (MAE, Spearman, start/sit, n,
  intervals); range coverage and width; FantasyPros row; forward results.
- New packages: scikit-learn, lightgbm.
- Stop after the 2025 benchmark for review, before anything is promoted.

## Amendment, Oct 21, 2026 (agreed with Codex before any training)

The rules above stay frozen; these replace or tighten the named sections.

- **Forward log: inputs saved, not just referenced.** Each forward prediction stores the
  feature values it used, the cutoff, and the SHA-256 of the model artifact. All inputs
  are read inside one REPEATABLE READ transaction. The snapshot time recorded is taken
  from the database (`clock_timestamp()`) by the transaction's first query, which is what
  fixes the snapshot. Model files live in `data/models/` (not in git) and are backed up
  outside the repo. A hash identifies a file but can't recover a lost one.
- **Timeliness by freeze time.** Ingestion finish, snapshot time and prediction finish
  are stored separately. A run is on time when its snapshot was taken before the Tuesday
  12:00 ET deadline, whenever the prediction itself finished. A later snapshot is stored
  as late and left out of on-time forward results; a Tuesday with no run is reported as
  missing. A scheduled task ingests and predicts on Tuesday mornings (10:30 ET).
- **The real information cutoff is the snapshot time.** A 10:45 snapshot is reported as
  "data as of 10:45, deadline Tuesday noon", never as everything available by noon.
- **Ranges: explicit order statistics.** With calibration residuals r = actual − predicted
  sorted ascending as r(1) ≤ … ≤ r(n) (one-based, no interpolation): lower = prediction +
  r(⌊(n+1)·0.1⌋), upper = prediction + r(⌈(n+1)·0.9⌉). With n < 50, or when either index
  falls outside 1..n, no range is shown. 80% is nominal coverage under exchangeability;
  rolling retraining and a changing season break that assumption, so coverage and width
  are measured, and those numbers, not the formula, decide whether a range ships.
  Fantasy points can be negative.
- **Training labels pass the same rule as features:** a game's stats are a training
  label for cutoff c only if the game kicked off at least 12 hours before c ("earlier
  week" alone misses postponed games).
- **Calibration per policy:** each evaluated policy (baseline; Ridge with baseline
  fallback; LightGBM with baseline fallback) calibrates on its own residuals only.
- **FantasyPros:** every comparator, the baseline included, is scored on the same subset
  of FantasyPros-ranked players and pairs.
- **Rare components:** two-point conversions and return / fumble-recovery TDs are
  projected by the player's exponentially weighted average, not a trained model (too few
  events per position to fit).

## Implementation notes, Oct 21, 2026 (after the benchmark)

- **Result (frozen rule, PPR 2025):** only Ridge for TE qualifies (MAE −2.4%, 95% CI of the
  difference −0.16 to −0.02; Spearman 0.623 vs 0.605; start/sit 60.6% vs 59.4%; half and
  standard also better). QB, RB and WR stay on the baseline; LightGBM qualifies nowhere.
  Ranges cover 77–82% in every policy, position and format. Full report:
  `data/logs/benchmark_20261021T112422Z.json` (local).
- **Activation is a separate, reviewed step.** `ml/serve.py` `ACTIVE` is empty: production
  serves the baseline everywhere. Ridge is logged forward for TE every Tuesday regardless.
- **Model artifacts** are stored in Postgres (`model_artifacts`, insert-only, by SHA-256)
  instead of files in `data/models/`: they live and are backed up with the database, never
  in git. Each forward run references the hash.
- **Week 7 of 2026** has two forward runs, both recorded `late` (made Oct 21 to test the
  pipeline end to end, after the Oct 20 deadline); they are never counted.
- XGBoost and CatBoost stay deferred: adding challengers against the already examined 2025
  benchmark would weaken any new win; they'd be judged on forward weeks instead.

## Review fixes, Oct 21, 2026

- **Trained points keep every league rule.** A challenger's points are the baseline's
  plus the change in the components it predicts, scored with each rule set's linear
  keys. Anything it doesn't predict (special-teams `st_ff`/`st_fum_rec`, targets, first
  downs, threshold bonuses) counts exactly as in the baseline. For the presets this equals
  the previous calculation.
- **Calibration is frozen, not rebuilt.** `calibration_errors` (insert-only, migration
  0009) holds the seed, recorded once on Oct 21, 2026 (`fantasy-rag calibration-seed`;
  a second seed is refused): each policy's out-of-sample errors from 2024 to week 6 of 2026.
  From then on it grows only from saved forward predictions. **Outcome-finalization policy:**
  a week is final 4 days after its last game (after the NFL's Monday–Wednesday stat
  corrections). Its first on-time run's predictions are then scored once against the
  stats at that moment, for players who played. A recorded error never changes. Forward
  ranges read only this table.
- **Fallbacks say so.** `projections.effective_policy` records the policy a row really
  used: a Ridge row for a player with fewer than 3 games is `baseline`, carries the
  baseline's range, and is served as the baseline. Ridge's own errors come only from rows
  where Ridge predicted.
