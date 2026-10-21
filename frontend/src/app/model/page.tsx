import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import Link from "next/link";

import { type ForwardResults, type Metrics, type ModelRun, type PolicyResult, type Scoring, api, scoringOf } from "@/lib/api";

export const dynamic = "force-dynamic";

const POSITIONS = ["QB", "RB", "WR", "TE"];
const POLICY_NAMES: Record<string, string> = { baseline: "Baseline", ridge: "Ridge", lgbm: "LightGBM" };
const pct = (v: number | null | undefined) => (v == null ? "–" : `${(v * 100).toFixed(1)}%`);
const num = (v: number | null | undefined, d = 3) => (v == null ? "–" : v.toFixed(d));
const ci = (v?: [number, number] | [null, null]) => (v && v[0] != null ? `${num(v[0], 2)} to ${num(v[1], 2)}` : "–");

/** Phase 8 benchmark: the ridge and lgbm runs each carry the baseline on the same rows. */
function benchmark(runs: ModelRun[]): Record<string, Record<string, PolicyResult>> | null {
  const ridge = runs.find((r) => r.model_version === "ridge-components-v1");
  const lgbm = runs.find((r) => r.model_version === "lgbm-components-v1");
  if (!ridge?.metrics.ppr || !lgbm?.metrics.ppr) return null;
  const out: Record<string, Record<string, PolicyResult>> = {};
  for (const f of ["ppr", "half", "std"] as const) {
    out[f] = { baseline: ridge.metrics[f]!.baseline, ridge: ridge.metrics[f]!.ridge, lgbm: lgbm.metrics[f]!.lgbm };
  }
  return out;
}

function BenchmarkTable({ ppr }: { ppr: Record<string, PolicyResult> }) {
  return (
    <Table aria-label="Benchmark">
      <TableHeader>
        <TableRow>
          <TableHead>Position</TableHead>
          <TableHead>Policy</TableHead>
          <TableHead className="text-right">MAE</TableHead>
          <TableHead className="text-right">vs baseline (95% CI)</TableHead>
          <TableHead className="text-right">Spearman</TableHead>
          <TableHead className="text-right">Start/sit</TableHead>
          <TableHead className="text-right">Rows / pairs</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {POSITIONS.flatMap((pos) =>
          ["baseline", "ridge", "lgbm"].map((k) => {
            const m: Metrics = ppr[k].by_position[pos];
            const base = ppr.baseline.by_position[pos];
            const change = k === "baseline" || m.mae == null || base.mae == null ? null : (m.mae - base.mae) / base.mae;
            return (
              <TableRow key={`${pos}-${k}`} className={k === "baseline" ? "border-t-2" : "text-muted-foreground"}>
                <TableCell className="font-medium">{k === "baseline" ? pos : ""}</TableCell>
                <TableCell>{POLICY_NAMES[k]}</TableCell>
                <TableCell className="text-right tabular-nums">{num(m.mae)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {change == null ? "–" : `${(change * 100).toFixed(1)}% (${ci(ppr[k].vs_baseline_ci?.[pos].mae)})`}
                </TableCell>
                <TableCell className="text-right tabular-nums">{num(m.spearman)}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(m.start_sit)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {m.n} / {m.start_sit_pairs}
                </TableCell>
              </TableRow>
            );
          }),
        )}
      </TableBody>
    </Table>
  );
}

const STATUS_TEXT: Record<string, string> = {
  on_time: "on time",
  late: "late (not counted)",
  early_only: "early only (not counted)",
  missing: "missing",
};

function ForwardResultsSection({ fr, scoring }: { fr: ForwardResults; scoring: Scoring }) {
  return (
    <section className="space-y-2" aria-label="Forward results">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">Forward results (unseen 2026 weeks)</h2>
        <div className="flex gap-3 text-sm">
          {(["ppr", "half", "std"] as const).map((s) => (
            <Link
              key={s}
              href={`/model?scoring=${s}`}
              className={s === scoring ? "font-semibold underline" : "text-muted-foreground hover:underline"}
            >
              {s.toUpperCase()}
            </Link>
          ))}
        </div>
      </div>
      {fr.rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No finalized weeks yet. A week counts once its on-time Tuesday run exists and its last game is 4 days old
          (the first is week 8, final November 6).
        </p>
      ) : (
        <>
          <p className="text-sm">
            TE, baseline vs Ridge on the same {fr.matched_total.n} player-weeks: MAE {fr.matched_total.baseline_mae ?? "–"} vs{" "}
            {fr.matched_total.ridge_mae ?? "–"} ({scoring.toUpperCase()}).
          </p>
          <Table aria-label="Forward results by week">
            <TableHeader>
              <TableRow>
                <TableHead>Week</TableHead>
                <TableHead>Position</TableHead>
                <TableHead className="text-right">Baseline MAE (n)</TableHead>
                <TableHead className="text-right">Matched: baseline / Ridge (n)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fr.rows.map((r) => (
                <TableRow key={`${r.season}-${r.week}-${r.position}`}>
                  <TableCell>{r.week}</TableCell>
                  <TableCell>{r.position}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {r.baseline_mae ?? "–"} ({r.n})
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {r.matched_n ? `${r.matched_baseline_mae} / ${r.matched_ridge_mae} (${r.matched_n})` : "–"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
      <ul className="flex flex-wrap gap-3 text-sm" aria-label="Tuesday status">
        {fr.tuesdays.map((t) => (
          <li key={`${t.season}-${t.week}`} className={t.status === "on_time" ? "" : "text-destructive"}>
            wk {t.week}: {STATUS_TEXT[t.status]}
          </li>
        ))}
      </ul>
    </section>
  );
}

export default async function ModelPage({ searchParams }: PageProps<"/model">) {
  const scoring = scoringOf((await searchParams).scoring);
  const [runs, forward, fr] = await Promise.all([api.modelRuns(), api.forwardRuns(), api.forwardResults(scoring)]);
  const bench = benchmark(runs);
  const ridge = runs.find((r) => r.model_version === "ridge-components-v1");
  const promotion = ridge?.metrics.promotion ?? {};
  const fp = ridge?.metrics.fantasypros as
    | ({ rows: number; of: number } & Record<string, { spearman: number; start_sit: number; start_sit_pairs: number }>)
    | undefined;
  const serving = (pos: string) => forward.active[pos] ?? "baseline";
  const legacy = runs.filter((r) => !r.model_version.endsWith("-components-v1"));

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Model performance</h1>
      <p className="text-sm" aria-label="Serving policy">
        Projections now come from{" "}
        {POSITIONS.map((p) => `${p}: ${POLICY_NAMES[serving(p)] ?? serving(p)}`).join(" · ")}.{" "}
        {Object.entries(promotion)
          .filter(([pos, d]) => d.winner && serving(pos) === "baseline")
          .map(([pos, d]) => `${POLICY_NAMES[d.winner!]} qualified for ${pos} under the frozen rule; it serves ${pos} only after its serving code is reviewed.`)
          .join(" ")}
      </p>

      {bench ? (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">2025 benchmark (PPR)</h2>
          <p className="text-sm text-muted-foreground">
            Every model was retrained each week on games known by that week&apos;s Tuesday 12:00 ET cutoff and
            scored on the same players and start/sit pairs. Gains over the simple baseline are small: Ridge for TE
            cut the average miss by about 0.09 points.
          </p>
          <BenchmarkTable ppr={bench.ppr} />
          <p className="text-sm text-muted-foreground" aria-label="Promotion">
            Frozen rule (set before training): MAE at least 2% lower with its 95% interval below zero, and higher
            Spearman and start/sit accuracy; a winner worse in half or standard scoring stays on the baseline.{" "}
            {Object.entries(promotion)
              .map(([pos, d]) => `${pos}: ${d.winner ? `${POLICY_NAMES[d.winner]} qualifies` : "baseline stays"}`)
              .join(" · ")}
            .
          </p>
        </section>
      ) : null}

      {bench ? (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">80% ranges (2025, serving policy)</h2>
          <Table aria-label="Range coverage">
            <TableHeader>
              <TableRow>
                <TableHead>Scoring</TableHead>
                {POSITIONS.map((p) => (
                  <TableHead key={p} className="text-right">
                    {p} ({POLICY_NAMES[serving(p)]})
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {(["ppr", "half", "std"] as const).map((f) => (
                <TableRow key={f}>
                  <TableCell className="uppercase">{f}</TableCell>
                  {POSITIONS.map((p) => {
                    const r = bench[f][serving(p)].ranges[p];
                    return (
                      <TableCell key={p} className="text-right tabular-nums">
                        {pct(r.coverage)} · {num(r.width, 1)} pts wide
                      </TableCell>
                    );
                  })}
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="text-sm text-muted-foreground">
            Coverage is the share of 2025 outcomes inside the range (target 75–85%; 80% is nominal, so it is
            measured, not promised). Ranges come from each policy&apos;s own past errors. Wide ranges mean small
            projection differences deserve cautious start/sit calls.
          </p>
        </section>
      ) : null}

      {fp ? (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Against FantasyPros (PPR rankings)</h2>
          <Table aria-label="FantasyPros">
            <TableHeader>
              <TableRow>
                <TableHead />
                <TableHead className="text-right">Spearman</TableHead>
                <TableHead className="text-right">Start/sit</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {["fantasypros", "baseline", "ridge", "lgbm"].map((k) => (
                <TableRow key={k}>
                  <TableCell>{k === "fantasypros" ? "FantasyPros consensus" : POLICY_NAMES[k]}</TableCell>
                  <TableCell className="text-right tabular-nums">{num(fp[k]?.spearman)}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(fp[k]?.start_sit)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="text-sm text-muted-foreground">
            Same {fp.rows} of {fp.of} player-weeks and the same pairs for everyone. FantasyPros rankings were
            captured on Fridays, three days after our Tuesday cutoff, so they had more news; this is not a
            like-for-like contest.
          </p>
        </section>
      ) : null}

      <ForwardResultsSection fr={fr} scoring={scoring} />

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Forward runs (2026)</h2>
        {forward.runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No forward run yet; the first is Tuesday, October 27.</p>
        ) : (
          <Table aria-label="Forward runs">
            <TableHeader>
              <TableRow>
                <TableHead>Week</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Data as of</TableHead>
                <TableHead>Deadline</TableHead>
                <TableHead className="text-right">Players</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {forward.runs.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    {r.season} wk {r.week}
                  </TableCell>
                  <TableCell>{r.status === "on_time" ? "on time" : `${r.status} (not counted)`}</TableCell>
                  <TableCell>{new Date(r.snapshot_at).toLocaleString()}</TableCell>
                  <TableCell>{new Date(r.cutoff_at).toLocaleString()}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.players}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <p className="text-sm text-muted-foreground">
          Each Tuesday the baseline and TE Ridge are predicted from the same saved inputs and stored unchanged.
          Only runs whose data snapshot was taken before the noon ET deadline count; these weeks are the unseen
          test the 2025 benchmark can&apos;t be.
        </p>
      </section>

      <section className="space-y-1 text-sm text-muted-foreground" aria-label="Caveats">
        <h2 className="text-lg font-semibold text-foreground">What these numbers assume</h2>
        <p>
          Past seasons assume each week&apos;s stats and snap counts were published by Tuesday noon, which can&apos;t be
          verified now, and use corrected stats a Tuesday user wouldn&apos;t have had. Players are scored only for
          games they played. 2025 was already seen while building the baseline, so it is a benchmark, not an
          untouched test. Betting lines and injury news aren&apos;t model inputs yet.
        </p>
      </section>

      {legacy.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Baseline tuning (Phase 4)</h2>
          <Table aria-label="Model runs">
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Test season</TableHead>
                <TableHead className="text-right">MAE (pts)</TableHead>
                <TableHead className="text-right">Start/sit accuracy</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {legacy.map((r) => (
                <TableRow key={r.model_version}>
                  <TableCell className="font-mono text-xs">{r.model_version}</TableCell>
                  <TableCell>{r.metrics.test?.season ?? "–"}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.metrics.test?.mae ?? "–"}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(r.metrics.test?.start_sit_accuracy)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      ) : null}
    </div>
  );
}
