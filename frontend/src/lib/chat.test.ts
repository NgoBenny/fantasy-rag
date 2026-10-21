import { describe, expect, it } from "vitest";

import { parseEvents } from "./chat";

const progress = 'id: 1\nevent: progress\ndata: {"demo":false,"stage":"context","message":"Reading","elapsed_ms":0}\n\n';
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
