import { afterEach, describe, expect, it, vi } from "vitest";

import { type ChatEvent, parseEvents, streamChat } from "./chat";

const progress = 'id: 1\nevent: progress\ndata: {"demo":false,"stage":"context","message":"Reading","elapsed_ms":0}\n\n';
const withheld = 'id: 2\nevent: withheld\ndata: {"demo":false,"message":"Not shown","reason":"verification_failed"}\n\n';
const done = 'id: 2\nevent: done\ndata: {"demo":false,"query_id":7,"logged":true,"progress_first_ms":0,"answer_ms":5}\n\n';

describe("parseEvents", () => {
  it("parses complete events and keeps a partial one for the next chunk", () => {
    const split = progress.length + 10;
    const first = parseEvents((progress + done).slice(0, split));
    expect(first.events.map((e) => e.event)).toEqual(["progress"]);
    const second = parseEvents(first.rest + (progress + done).slice(split));
    expect(second.events.map((e) => e.event)).toEqual(["done"]);
    expect(second.rest).toBe("");
  });
  it("handles CRLF line endings", () => {
    const { events } = parseEvents(progress.replace(/\n/g, "\r\n"));
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe("progress");
  });
});

function serve(body: string) {
  vi.stubGlobal("fetch", async () => new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
}

async function run(): Promise<ChatEvent[]> {
  const seen: ChatEvent[] = [];
  await streamChat({ question: "q", league_id: null }, (e) => seen.push(e), new AbortController().signal);
  return seen;
}

describe("streamChat", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("resolves once a terminal event and done arrive", async () => {
    serve(progress + withheld + done);
    expect((await run()).map((e) => e.event)).toEqual(["progress", "withheld", "done"]);
  });
  it("throws when the stream ends after progress with no answer, withheld or error", async () => {
    serve(progress);
    await expect(run()).rejects.toThrow(/ended before/);
  });
  it("throws when the stream ends inside the terminal event", async () => {
    serve(progress + withheld.slice(0, 40));
    await expect(run()).rejects.toThrow(/ended before/);
  });
});
