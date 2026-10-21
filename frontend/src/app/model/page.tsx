import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

const pct = (v: number | undefined) => (v === undefined ? "–" : `${(v * 100).toFixed(1)}%`);

export default async function ModelPage() {
  const runs = await api.modelRuns();
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Model performance</h1>
      <p className="text-sm text-muted-foreground">
        Rolling-origin backtest of the projection baseline, tuned on one season and tested on the next.
        Trained models arrive in Phase 8 and ship only if they beat these numbers.
      </p>
      {runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No backtest recorded yet. Run <code>uv run fantasy-rag backtest</code>.
        </p>
      ) : (
        <Table aria-label="Model runs">
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead>Test season</TableHead>
              <TableHead className="text-right">MAE (pts)</TableHead>
              <TableHead className="text-right">Start/sit accuracy</TableHead>
              <TableHead>Recorded</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.flatMap((r) => [
              <TableRow key={r.model_version}>
                <TableCell className="font-mono text-xs">{r.model_version}</TableCell>
                <TableCell>{r.metrics.test?.season ?? "–"}</TableCell>
                <TableCell className="text-right tabular-nums">{r.metrics.test?.mae ?? "–"}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(r.metrics.test?.start_sit_accuracy)}</TableCell>
                <TableCell>{new Date(r.created_at).toLocaleDateString()}</TableCell>
              </TableRow>,
              ...Object.entries(r.metrics.naive ?? {}).map(([name, m]) => (
                <TableRow key={`${r.model_version}-${name}`} className="text-muted-foreground">
                  <TableCell className="pl-6 text-xs">naive: {name}</TableCell>
                  <TableCell>{r.metrics.test?.season ?? "–"}</TableCell>
                  <TableCell className="text-right tabular-nums">{m.mae}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(m.start_sit_accuracy)}</TableCell>
                  <TableCell />
                </TableRow>
              )),
            ])}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
