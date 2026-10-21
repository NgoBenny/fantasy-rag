// Same-origin proxy for the chat stream: the browser never talks to FastAPI directly.
// The client's abort (leaving the page, Stop) is forwarded, so the backend stops
// starting new model calls. If the API is unreachable, the stream still follows the
// contract: one error event, then done.
const API_URL = process.env.API_URL ?? "http://localhost:8010";

function unreachable(): Response {
  const events = [
    ["error", { demo: false, code: "internal", message: "The API is unreachable right now.", retryable: true }],
    ["done", { demo: false, query_id: null, logged: false, progress_first_ms: null, answer_ms: null }],
  ] as const;
  const body = events.map(([e, d], i) => `id: ${i + 1}\nevent: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

export async function POST(request: Request): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: await request.text(),
      signal: request.signal,
      cache: "no-store",
    });
  } catch {
    return unreachable();
  }
  if (!upstream.ok || !upstream.body) return unreachable();
  return new Response(upstream.body, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });
}
