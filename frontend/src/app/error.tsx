"use client";

import { Button } from "@/components/ui/button";

// `retry` re-fetches the failed server segment (e.g. after the API comes back);
// `reset` would only re-render the stale error.
export default function ErrorPage({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div role="alert" className="space-y-3">
      <h1 className="text-lg font-semibold">Something went wrong</h1>
      <p className="text-sm text-muted-foreground">
        {error instanceof Error ? error.message : "Unknown error"}
      </p>
      <Button variant="outline" onClick={() => retry()}>
        Try again
      </Button>
    </div>
  );
}
