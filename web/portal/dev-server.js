#!/usr/bin/env node
// Plain Node HTTP dev server for the portal SPA.
//
// - Serves files from this directory (./)
// - Proxies /v1/* to GATEWAY_URL (default http://localhost:8000)
// - Falls back to /index.html for unknown paths so hash routing works
//
// No bundler. Reload the browser to see changes.

import http from "node:http";
import { createReadStream, statSync } from "node:fs";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env.PORT || 3001);
const GATEWAY_URL = process.env.GATEWAY_URL || "http://localhost:8000";

const ROOT = resolve(fileURLToPath(import.meta.url), "..");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".map": "application/json",
};

function proxy(req, res) {
  const target = new URL(req.url, GATEWAY_URL);
  const headers = { ...req.headers, host: target.host };
  const upstream = http.request(
    {
      hostname: target.hostname,
      port: target.port,
      path: target.pathname + target.search,
      method: req.method,
      headers,
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );
  upstream.on("error", (err) => {
    res.writeHead(502, { "Content-Type": "text/plain" });
    res.end(`Upstream error: ${err.message}`);
  });
  req.pipe(upstream);
}

function serveFile(res, filePath) {
  const ext = extname(filePath).toLowerCase();
  res.writeHead(200, {
    "Content-Type": MIME[ext] || "application/octet-stream",
    "Cache-Control": "no-cache",
  });
  createReadStream(filePath).pipe(res);
}

function safeJoin(base, urlPath) {
  const resolved = normalize(join(base, urlPath));
  if (!resolved.startsWith(base)) return null;
  return resolved;
}

const server = http.createServer((req, res) => {
  const url = req.url || "/";

  // API + sibling SPA assets get proxied to the gateway.
  if (url.startsWith("/v1/") || url.startsWith("/admin/")) {
    return proxy(req, res);
  }

  // Map the SPA's absolute prefix to the local dev-server's files so URLs
  // are identical in dev and prod. (Index.html references /portal/portal.css.)
  let pathOnly = url.split("?")[0];
  if (pathOnly === "/" || pathOnly === "/portal" || pathOnly === "/portal/") {
    pathOnly = "/index.html";
  } else if (pathOnly.startsWith("/portal/")) {
    pathOnly = pathOnly.slice("/portal".length);
  }

  const filePath = safeJoin(ROOT, pathOnly);
  if (filePath) {
    try {
      const stat = statSync(filePath);
      if (stat.isFile()) return serveFile(res, filePath);
    } catch {
      /* fall through to index.html */
    }
  }
  return serveFile(res, join(ROOT, "index.html"));
});

server.listen(PORT, () => {
  console.log(`portal dev server on http://localhost:${PORT}`);
  console.log(`proxying /v1/* to ${GATEWAY_URL}`);
});
