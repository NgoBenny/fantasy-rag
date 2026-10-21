"use client";

import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { League } from "@/lib/api";
import { type Answer, type ChatEvent, type Source, streamChat } from "@/lib/chat";

type Outcome =
  | { kind: "answer"; answer: Answer; sources: Source[]; status: string }
  | { kind: "withheld"; message: string }
  | { kind: "error"; message: string; retryable: boolean };

function SourceList({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null;
  return (
    <ol aria-label="Sources" className="list-decimal space-y-1 pl-5 text-sm">
      {sources.map((s) => (
        <li key={s.id}>
          {s.failed ? (
            <span className="text-muted-foreground">{s.label ?? "Lookup failed"}</span>
          ) : s.href ? (
            <a
              href={s.href}
              className="underline"
              {...(s.kind === "news" ? { target: "_blank", rel: "noreferrer" } : {})}
            >
              {s.label ?? s.id}
            </a>
          ) : (
            <span>{s.label ?? s.id}</span>
          )}
          {s.kind === "news" && s.published_at ? (
            <span className="text-muted-foreground"> · {new Date(s.published_at).toLocaleDateString()}</span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function Claims({ title, items }: { title: string; items: Answer["facts"] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="text-sm font-medium">{title}</h3>
      <ul className="list-disc pl-5 text-sm">
        {items.map((c, i) => (
          <li key={i}>
            {c.text} <span className="text-muted-foreground">[{c.source_ids.join(", ")}]</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ChatClient({
  leagues,
  samples,
}: {
  leagues: League[];
  samples: { question: string; scenario: boolean }[];
}) {
  const [question, setQuestion] = useState("");
  const [leagueId, setLeagueId] = useState<number | null>(null);
  const [progress, setProgress] = useState<string[]>([]);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState<{ question: string; league_id: number | null } | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => () => abort.current?.abort(), []); // leaving the page stops the request

  async function ask(q: string, league: number | null) {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setAsked({ question: q, league_id: league });
    setProgress([]);
    setOutcome(null);
    setBusy(true);
    const onEvent = (e: ChatEvent) => {
      if (e.event === "progress") setProgress((p) => [...p, e.data.message]);
      else if (e.event === "answer")
        setOutcome({ kind: "answer", answer: e.data.answer, sources: e.data.sources, status: e.data.status });
      else if (e.event === "withheld") setOutcome({ kind: "withheld", message: e.data.message });
      else if (e.event === "error")
        setOutcome({ kind: "error", message: e.data.message, retryable: e.data.retryable });
    };
    try {
      await streamChat({ question: q, league_id: league }, onEvent, ctrl.signal);
    } catch {
      if (!ctrl.signal.aborted)
        setOutcome({ kind: "error", message: "The connection was lost.", retryable: true });
    } finally {
      if (abort.current === ctrl) setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) void ask(question.trim(), leagueId);
        }}
      >
        <Input
          aria-label="Question"
          value={question}
          maxLength={500}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. bucky irving or jaylen warren this week?"
          className="min-w-64 flex-1"
        />
        <select
          aria-label="League"
          className="rounded border bg-background px-2 text-sm"
          value={leagueId ?? ""}
          onChange={(e) => setLeagueId(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">No league selected</option>
          {leagues.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name ?? `League ${l.id}`}
            </option>
          ))}
        </select>
        <Button type="submit" disabled={busy}>
          {busy ? "Answering…" : "Ask"}
        </Button>
      </form>

      {samples.length > 0 && (
        <div className="flex flex-wrap gap-2" aria-label="Sample questions">
          {samples.map((s) => (
            <Button
              key={s.question}
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setQuestion(s.question);
                void ask(s.question, leagueId);
              }}
            >
              {s.scenario ? `Test scenario: ${s.question.replace(/^Demo scenario: /, "")}` : s.question}
            </Button>
          ))}
        </div>
      )}

      {progress.length > 0 && (
        <ul aria-label="Progress" className="space-y-1 text-sm text-muted-foreground">
          {progress.map((m, i) => (
            <li key={i}>{m}</li>
          ))}
        </ul>
      )}

      {outcome?.kind === "answer" && (
        <Card aria-label="Answer">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              Answer
              {outcome.answer.answer_type === "clarification" && <Badge variant="outline">needs more detail</Badge>}
              {outcome.status === "repaired" && <Badge variant="outline">verified after one fix</Badge>}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p>{outcome.answer.answer}</p>
            <Claims title="Facts" items={outcome.answer.facts} />
            <Claims title="Projections" items={outcome.answer.projections} />
            {outcome.answer.limitations.length > 0 && (
              <div>
                <h3 className="text-sm font-medium">Limitations</h3>
                <ul className="list-disc pl-5 text-sm text-muted-foreground">
                  {outcome.answer.limitations.map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              </div>
            )}
            <SourceList sources={outcome.sources} />
          </CardContent>
        </Card>
      )}

      {outcome?.kind === "withheld" && (
        <Card aria-label="Withheld">
          <CardContent className="pt-6 text-sm">{outcome.message}</CardContent>
        </Card>
      )}

      {outcome?.kind === "error" && (
        <div role="alert" className="flex items-center gap-3 text-sm text-destructive">
          {outcome.message}
          {outcome.retryable && asked && (
            <Button variant="outline" size="sm" onClick={() => void ask(asked.question, asked.league_id)}>
              Retry
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
