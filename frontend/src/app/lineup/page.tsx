import { redirect } from "next/navigation";

// The lineup optimizer moved into My League; old links keep working.
export default async function LineupRedirect({ searchParams }: PageProps<"/lineup">) {
  const sp = await searchParams;
  const keep = new URLSearchParams({ view: "lineup" });
  for (const k of ["league", "roster"] as const) {
    const v = sp[k];
    if (typeof v === "string") keep.set(k, v);
  }
  redirect(`/league?${keep}`);
}
