"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { startTransition, useOptimistic } from "react";

const toggle = (picked: string[], id: string) =>
  picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];

/** Start/sit picks. Each click builds on the pending selection, not the last server render,
 * so a click made while the previous one is still loading can't drop a player. */
export function RosterPicker({
  players,
  picked,
  base,
}: {
  players: { id: string; name: string }[];
  picked: string[];
  base: string; // the page URL without `ids`
}) {
  const router = useRouter();
  // Reducer form: a pick still loading is re-applied when an earlier navigation lands.
  const [shown, pick] = useOptimistic(picked, toggle);
  const url = (ids: string[]) => (ids.length ? `${base}&ids=${ids.join(",")}` : base);
  return (
    <div className="flex flex-wrap gap-2 text-sm" aria-label="Roster players">
      {players.map((p) => (
        <Link
          key={p.id}
          href={url(toggle(shown, p.id))}
          onClick={(e) => {
            e.preventDefault();
            const next = url(toggle(shown, p.id));
            startTransition(() => {
              pick(p.id);
              router.push(next, { scroll: false });
            });
          }}
          className={`rounded border px-2 py-1 ${shown.includes(p.id) ? "bg-muted font-semibold" : ""}`}
        >
          {p.name}
        </Link>
      ))}
    </div>
  );
}
