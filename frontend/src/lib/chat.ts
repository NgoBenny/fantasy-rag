// Chat event types (the SSE contract in backend/src/fantasy_rag/api/chat.py) and a parser.

export interface Claim {
  text: string;
  source_ids: string[];
}

export interface Answer {
  answer: string;
  answer_type: string;
  season: number | null;
  week: number | null;
  scoring_format: string | null;
  data_as_of: string | null;
  model_version: string | null;
  facts: Claim[];
  projections: Claim[];
  citations: string[];
  limitations: string[];
}

export interface Source {
  id: string;
  kind: "tool" | "news";
  label: string | null;
  href: string | null;
  failed: boolean;
  tool?: string;
  published_at?: string | null;
  source?: string | null;
}

export type ChatEvent =
  | { event: "progress"; data: { demo: boolean; stage: string; message: string; elapsed_ms: number; tool?: string; ok?: boolean } }
  | { event: "answer"; data: { demo: boolean; status: "ok" | "repaired"; answer: Answer; sources: Source[] } }
  | { event: "withheld"; data: { demo: boolean; message: string; reason: string } }
  | { event: "error"; data: { demo: boolean; code: string; message: string; retryable: boolean } }
  | { event: "done"; data: { demo: boolean; query_id: number | null; logged: boolean; progress_first_ms: number | null; answer_ms: number | null; cost_usd?: number | null } };

/** Split buffered SSE text into complete events; returns them and the unfinished rest. */
export function parseEvents(buffer: string): { events: ChatEvent[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: ChatEvent[] = [];
  for (const block of blocks) {
    let event = "";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data.push(line.slice(6));
    }
    if (event && data.length) events.push({ event, data: JSON.parse(data.join("\n")) } as ChatEvent);
  }
  return { events, rest };
}

/** POST a question and call `onEvent` for each event; aborts with `signal`. */
export async function streamChat(
  body: { question: string; league_id: number | null },
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`chat request failed (${res.status})`);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const parsed = parseEvents(buffer + value);
    buffer = parsed.rest;
    parsed.events.forEach(onEvent);
  }
}
