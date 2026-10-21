import { type Status, api } from "@/lib/api";

const LABELS: Record<string, string> = {
  stats: "stats",
  injuries: "injuries",
  news: "news",
  rosters: "rosters",
  transactions: "adds/drops",
};

function age(hours: number): string {
  return hours < 1 ? `${Math.round(hours * 60)}m ago` : hours < 48 ? `${Math.round(hours)}h ago` : `${Math.round(hours / 24)}d ago`;
}

/** One chip per source that isn't fresh: failing (its last attempts failed) or stale. */
function SourceWarnings({ health }: { health?: Status["health"] }) {
  const bad = Object.entries(health ?? {}).filter(([, h]) => h.state !== "fresh");
  if (bad.length === 0) return null;
  return (
    <div role="status" aria-label="Source warnings" className="mx-auto mt-1 flex max-w-5xl flex-wrap gap-2">
      {bad.map(([k, h]) => (
        <span key={k} className="rounded bg-destructive/10 px-2 py-0.5 font-medium text-destructive">
          {LABELS[k] ?? k}: {h.state}
          {h.state === "failing" && h.last_error_at ? ` since ${new Date(h.last_error_at).toLocaleTimeString()}` : ""}
          {h.last_ok ? ` · last good ${new Date(h.last_ok).toLocaleString()}` : " · never succeeded"}
        </span>
      ))}
    </div>
  );
}

// Season, week and how fresh each kind of data is, on every page. Never breaks a page:
// if the API is down, it says so and the page's own error handling takes over.
export async function StatusHeader() {
  let s;
  try {
    s = await api.status();
  } catch {
    return (
      <div aria-label="Data status" className="border-b bg-muted/40 px-4 py-1 text-center text-xs text-destructive">
        API unreachable: data status unknown
      </div>
    );
  }
  return (
    <div aria-label="Data status" className="border-b bg-muted/40 px-4 py-1 text-xs text-muted-foreground">
      {s.demo && (
        <div role="note" className="mb-1 rounded bg-amber-100 px-2 py-1 text-center font-medium text-amber-900">
          Demo: sample data, not live. {s.label}
        </div>
      )}
      <div className="mx-auto flex max-w-5xl flex-wrap gap-x-4">
        <span>
          {s.season} season · stats through week {s.latest_week} · next games week {s.next_week}
        </span>
        {Object.entries(s.sources).map(([k, v]) => (
          <span key={k} className={v && Number(v.age_hours) > 24 ? "text-destructive" : undefined}>
            {LABELS[k] ?? k}: {v ? age(Number(v.age_hours)) : "never"}
          </span>
        ))}
      </div>
      <SourceWarnings health={s.health} />
    </div>
  );
}
