// Test-only pass-through to the real API (8011 -> 8010) that can simulate an outage:
// GET /__down makes every request fail with 503, GET /__up restores it.
import http from "node:http";

let down = false;
http
  .createServer((req, res) => {
    if (req.url === "/__down" || req.url === "/__up") {
      down = req.url === "/__down";
      return res.end(down ? "down" : "up");
    }
    if (down) {
      res.writeHead(503, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ detail: "simulated API outage" }));
    }
    const upstream = http.request(
      { host: "localhost", port: 8010, path: req.url, method: req.method, headers: req.headers },
      (r) => {
        res.writeHead(r.statusCode ?? 502, r.headers);
        r.pipe(res);
      },
    );
    upstream.on("error", () => res.writeHead(502).end());
    req.pipe(upstream);
  })
  .listen(8011);
