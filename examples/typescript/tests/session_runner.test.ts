import { describe, expect, it } from "vitest";
import { WebSocketServer } from "ws";

import {
  type CapStatus,
  OpenClearinghouseClient,
  OpenClearinghouseError,
  type RefillEvent,
  type SessionHandle,
  SessionRunner,
  type WinddownEvent,
} from "../src/index.js";

const BASE = "http://loc.test";
const KEY = "pymth_live_test";

function handle(brokerUrl: string, mode: string, sid = "00000000-0000-0000-0000-000000000aaa"): SessionHandle {
  return {
    sessionId: sid,
    workId: "wid",
    brokerUrl,
    mode,
    paymentEnvelope: "BASE64ENV",
    expectedValueWei: 100_000n,
    fundedValueWei: 200_000n,
    refillEndpoint: `/v1/sessions/${sid}/refill`,
    closeEndpoint: `/v1/sessions/${sid}/close`,
  };
}

interface FetchCall {
  url: string;
  init: RequestInit | undefined;
}

function makeFetch(routes: Record<string, (call: FetchCall) => Promise<Response> | Response>): {
  fetch: typeof fetch;
  calls: FetchCall[];
} {
  const calls: FetchCall[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    calls.push({ url, init });
    for (const [pattern, h] of Object.entries(routes)) {
      if (url.endsWith(pattern) || url.includes(pattern)) {
        return h({ url, init });
      }
    }
    return new Response(JSON.stringify({ detail: `unmatched ${url}` }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  };
  return { fetch: fetchImpl, calls };
}

function jsonResp(body: unknown, opts: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json", ...(opts.headers as Record<string, string>) },
    ...opts,
  });
}

const REFILL_RESPONSE = {
  work_id: "wid",
  refill_seq: 1,
  payment_envelope: "REFILL-ENV",
  expected_value_wei: 50_000,
  funded_value_wei: 50_000,
  cap_status: {
    session_pct_used: 0.4,
    spend_period_pct_used: null,
    user_balance_pct_used: null,
    operator_pool_pct_used: null,
    will_refuse_next_refill: false,
    winddown_reason: null,
  } as CapStatus,
};

async function startWsServer(handler: (ws: WebSocketServer) => void): Promise<{ url: string; close: () => Promise<void> }> {
  const server = new WebSocketServer({ port: 0 });
  await new Promise<void>((resolve) => server.once("listening", () => resolve()));
  handler(server);
  const addr = server.address();
  const port = typeof addr === "string" ? 0 : addr!.port;
  return {
    url: `ws://127.0.0.1:${port}`,
    close: () =>
      new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

describe("SessionRunner", () => {
  it("refills on balance-low for session-control-plus-media (WS topup)", async () => {
    const received: string[] = [];
    const ws = await startWsServer((server) => {
      server.on("connection", (sock) => {
        sock.send(
          JSON.stringify({ type: "session.balance.low", observed_consumed_units: 80 }),
        );
        sock.on("message", (m) => received.push(m.toString()));
      });
    });
    try {
      const sid = "11111111-1111-1111-1111-111111111111";
      const { fetch: locFetch } = makeFetch({
        [`/v1/sessions/${sid}/refill`]: () => jsonResp(REFILL_RESPONSE),
        [`/v1/sessions/${sid}/close`]: () =>
          jsonResp({
            session_id: sid,
            work_id: "wid",
            actual_units: 0,
            billed_value_wei: 0,
            refund_wei: 200_000,
            outcome: "OVERFUNDED",
            closed_at: "2026-05-24T12:30:00Z",
          }),
      });
      const client = new OpenClearinghouseClient({
        baseUrl: BASE,
        apiKey: KEY,
        fetch: locFetch,
      });

      let refillEvent: RefillEvent | null = null;
      const runner = new SessionRunner({
        client,
        handle: handle(ws.url, "session-control-plus-media@v0", sid),
        onRefillSucceeded: (e) => {
          refillEvent = e;
        },
      });
      await runner.start();
      // Wait briefly for the WS message dance to land
      await new Promise((r) => setTimeout(r, 200));
      await runner.close({ actualUnits: 0 });

      expect(refillEvent).not.toBeNull();
      expect(received).toHaveLength(1);
      const frame = JSON.parse(received[0]!);
      expect(frame.type).toBe("session.topup");
      expect(frame.body.payment_header).toBe("REFILL-ENV");
    } finally {
      await ws.close();
    }
  });

  it("ws-realtime: balance-low fires winddown only; no refill call", async () => {
    let refillCalled = false;
    const ws = await startWsServer((server) => {
      server.on("connection", (sock) => {
        sock.send(JSON.stringify({ type: "session.balance.low" }));
      });
    });
    try {
      const sid = "22222222-2222-2222-2222-222222222222";
      const { fetch: locFetch } = makeFetch({
        [`/v1/sessions/${sid}/refill`]: () => {
          refillCalled = true;
          return jsonResp({}, { status: 400 });
        },
        [`/v1/sessions/${sid}/close`]: () =>
          jsonResp({
            session_id: sid,
            work_id: "wid",
            actual_units: 0,
            billed_value_wei: 0,
            refund_wei: 200_000,
            outcome: "OVERFUNDED",
            closed_at: "2026-05-24T12:30:00Z",
          }),
      });
      const client = new OpenClearinghouseClient({
        baseUrl: BASE,
        apiKey: KEY,
        fetch: locFetch,
      });

      let wd: WinddownEvent | null = null;
      const runner = new SessionRunner({
        client,
        handle: handle(ws.url, "ws-realtime@v0", sid),
        onWinddownWarning: (e) => {
          wd = e;
        },
      });
      await runner.start();
      await new Promise((r) => setTimeout(r, 200));
      await runner.close({ actualUnits: 0 });

      expect(wd).not.toBeNull();
      expect(wd!.reason).toBe("ws_session_exhausting");
      expect(refillCalled).toBe(false);
    } finally {
      await ws.close();
    }
  });

  it("fires onRefillRefused when LOC returns 402", async () => {
    const ws = await startWsServer((server) => {
      server.on("connection", (sock) => {
        sock.send(JSON.stringify({ type: "session.balance.low" }));
      });
    });
    try {
      const sid = "33333333-3333-3333-3333-333333333333";
      const { fetch: locFetch } = makeFetch({
        [`/v1/sessions/${sid}/refill`]: () =>
          new Response(
            JSON.stringify({
              error: {
                code: "cap_reached",
                message: "period cap reached",
                details: { which: "spend_period", remaining_wei: "0" },
              },
            }),
            { status: 402, headers: { "Content-Type": "application/json" } },
          ),
        [`/v1/sessions/${sid}/close`]: () =>
          jsonResp({
            session_id: sid,
            work_id: "wid",
            actual_units: 0,
            billed_value_wei: 0,
            refund_wei: 200_000,
            outcome: "OVERFUNDED",
            closed_at: "2026-05-24T12:30:00Z",
          }),
      });
      const client = new OpenClearinghouseClient({
        baseUrl: BASE,
        apiKey: KEY,
        fetch: locFetch,
      });

      let refused: RefillEvent | null = null;
      const runner = new SessionRunner({
        client,
        handle: handle(ws.url, "session-control-plus-media@v0", sid),
        onRefillRefused: (e) => {
          refused = e;
        },
      });
      await runner.start();
      await new Promise((r) => setTimeout(r, 200));
      await runner.close({ actualUnits: 0 });

      expect(refused).not.toBeNull();
      expect(refused!.error).toBeInstanceOf(OpenClearinghouseError);
    } finally {
      await ws.close();
    }
  });

  it("unsupported mode raises at start", async () => {
    const sid = "44444444-4444-4444-4444-444444444444";
    const { fetch: locFetch } = makeFetch({
      [`/v1/sessions/${sid}/close`]: () =>
        jsonResp({
          session_id: sid,
          work_id: "wid",
          actual_units: 0,
          billed_value_wei: 0,
          refund_wei: 0,
          outcome: "OVERFUNDED",
          closed_at: "2026-05-24T12:30:00Z",
        }),
    });
    const client = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: locFetch,
    });
    const runner = new SessionRunner({
      client,
      handle: handle("http://broker.test", "http-reqresp@v0", sid),
    });
    await expect(runner.start()).rejects.toThrow(/unsupported mode/);
  });

  it("close populates outcome / billed / refund", async () => {
    const ws = await startWsServer(() => {
      /* no-op; idle */
    });
    try {
      const sid = "55555555-5555-5555-5555-555555555555";
      const { fetch: locFetch } = makeFetch({
        [`/v1/sessions/${sid}/close`]: () =>
          jsonResp({
            session_id: sid,
            work_id: "wid",
            actual_units: 80,
            billed_value_wei: 80_000,
            refund_wei: 120_000,
            outcome: "OVERFUNDED",
            closed_at: "2026-05-24T12:30:00Z",
          }),
      });
      const client = new OpenClearinghouseClient({
        baseUrl: BASE,
        apiKey: KEY,
        fetch: locFetch,
      });
      const runner = new SessionRunner({
        client,
        handle: handle(ws.url, "session-control-plus-media@v0", sid),
      });
      await runner.start();
      await runner.close({ actualUnits: 80 });
      expect(runner.outcome).toBe("OVERFUNDED");
      expect(runner.billedValueWei).toBe(80_000n);
      expect(runner.refundWei).toBe(120_000n);
    } finally {
      await ws.close();
    }
  });
});
