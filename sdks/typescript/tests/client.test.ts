import { describe, expect, it } from "vitest";
import {
  BrokerProtocolError,
  InsufficientCredit,
  NoRouteAvailable,
  OpenClearinghouseClient,
} from "../src/index.js";

// --- helpers --------------------------------------------------------------

const BASE = "http://loc.test";
const BROKER = "https://broker.example/livepeer";
const KEY = "pymth_live_test";
const SIGNED_SETTLEMENT = { payload: {}, signature: {} };
const ENCODED_SETTLEMENT = btoa(JSON.stringify(SIGNED_SETTLEMENT));

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
    for (const [pattern, handler] of Object.entries(routes)) {
      if (url.endsWith(pattern) || url.includes(pattern)) {
        return handler({ url, init });
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
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers as Record<string, string>),
    },
    ...opts,
  });
}

const JOB_OPEN = {
  job_id: "00000000-0000-0000-0000-000000000abc",
  request_id: "broker-request-1",
  work_id: "wid-abc",
  broker_url: BROKER,
  protocol: "paid-job/v1",
  transport: "unary" as const,
  work_unit: "token",
  payment_envelope: "BASE64ENV",
  expected_value_wei: 100_000,
  funded_value_wei: 100_000,
  settle_endpoint: "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
  opened_at: "2026-05-24T12:00:00Z",
};

function paidJobHeaders(units: number): Record<string, string> {
  return {
    "Livepeer-Work-Units": String(units),
    "Livepeer-Work-Unit": "token",
    "Livepeer-Job-Id": "broker-job-1",
    "Livepeer-Settlement": ENCODED_SETTLEMENT,
  };
}

function settledFor(actual: number) {
  return {
    job_id: JOB_OPEN.job_id,
    work_id: JOB_OPEN.work_id,
    actual_units: actual,
    billed_value_wei: actual * 1000,
    refund_wei: 100_000 - actual * 1000,
    outcome: "OVERFUNDED",
    closed_at: "2026-05-24T12:00:30Z",
    cap_status: {
      session_pct_used: actual / 100,
      spend_period_pct_used: null,
      user_balance_pct_used: null,
      operator_pool_pct_used: null,
      will_refuse_next_refill: false,
      winddown_reason: null,
    },
  };
}

// --- tests ----------------------------------------------------------------

describe("OpenClearinghouseClient", () => {
  it("rejects an obviously wrong api key", () => {
    expect(() => new OpenClearinghouseClient({ baseUrl: BASE, apiKey: "nope" })).toThrow(
      /looks wrong/,
    );
  });

  it("attaches the SDK identity header on every request", async () => {
    const { fetch, calls } = makeFetch({
      "/v1/capabilities": () => jsonResp({ items: [] }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await client.listCapabilities();
    const headers = (calls[0]?.init?.headers ?? {}) as Record<string, string>;
    expect(headers["Livepeer-Open-Clearinghouse-SDK"]).toMatch(/^typescript\//);
  });

  it("submitJob does mint + broker + settle and returns the broker body", async () => {
    const { fetch, calls } = makeFetch({
      "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle": () => jsonResp(settledFor(42)),
      "/v1/jobs": () => jsonResp(JOB_OPEN, { status: 201 }),
      "/v1/job": () =>
        new Response(JSON.stringify({ reply: "ok" }), {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            ...paidJobHeaders(42),
          },
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const result = await client.submitJob({
      capability: "openai:chat-completions",
      offering: "gpt-oss-20b",
      estimatedUnits: 80,
      maxTotalUnits: 100,
      body: { prompt: "hello" },
    });
    expect(result.actualUnits).toBe(42);
    expect(result.billedValueWei).toBe(42_000n);
    expect(result.refundWei).toBe(58_000n);
    expect(result.outcome).toBe("OVERFUNDED");
    expect(result.body).toEqual({ reply: "ok" });
    expect(result.protocol).toBe("paid-job/v1");
    expect(result.transport).toBe("unary");
    expect(result.workUnit).toBe("token");
    expect(result.brokerJobId).toBe("broker-job-1");
    expect(result.requestId).toBe("broker-request-1");
    expect(calls.map((c) => c.url)).toEqual([
      `${BASE}/v1/jobs`,
      `${BROKER}/v1/job`,
      `${BASE}${JOB_OPEN.settle_endpoint}`,
    ]);
    const openHeaders = calls[0]?.init?.headers as Record<string, string>;
    expect(openHeaders["Idempotency-Key"]).toBeTruthy();
    const settleBody = calls[2]?.init?.body;
    if (typeof settleBody !== "string") throw new TypeError("expected JSON settle body");
    expect(JSON.parse(settleBody)).toEqual({
      actual_units: 42,
      broker_job_id: "broker-job-1",
      work_unit: "token",
      settlement: SIGNED_SETTLEMENT,
    });
  });

  it("submitJob queries a streamed terminal claim and signed settlement", async () => {
    const streamJob = { ...JOB_OPEN, transport: "stream" as const };
    const signedSettlement = {
      payload: { work_id: "wid-abc", debited_units: "7" },
      signature: {
        algorithm: "secp256k1",
        canonicalization: "jcs",
        value: "0xsigned",
      },
    };
    const encodedSettlement = btoa(JSON.stringify(signedSettlement));
    const { fetch, calls } = makeFetch({
      "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle": () => jsonResp(settledFor(7)),
      "/v1/jobs": () => jsonResp(streamJob, { status: 201 }),
      "/v1/settlement/broker-job-1": () =>
        jsonResp(
          {
            job_id: "broker-job-1",
            state: "terminal",
            work_units: 7,
            unit: "token",
          },
          {
            headers: {
              ...paidJobHeaders(7),
              "Livepeer-Settlement": encodedSettlement,
            },
          },
        ),
      "/v1/job": () =>
        new Response("data: hello\n\n", {
          status: 200,
          headers: {
            "Content-Type": "text/event-stream",
            "Livepeer-Job-Id": "broker-job-1",
            "Livepeer-Work-Unit": "token",
            Trailer: "Livepeer-Work-Units, Livepeer-Settlement",
          },
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const result = await client.submitJob({
      capability: "openai:chat-completions",
      offering: "gpt-oss-20b",
      estimatedUnits: 10,
      body: { prompt: "hello" },
      transport: "stream",
    });

    expect(result.body).toBe("data: hello\n\n");
    expect(result.actualUnits).toBe(7);
    expect(calls.map((call) => call.url)).toContain(`${BROKER}/v1/settlement/broker-job-1`);
    const brokerCall = calls.find((call) => call.url.endsWith("/v1/job"));
    expect((brokerCall?.init?.headers as Record<string, string>).Accept).toBe("text/event-stream");
    const settleCall = calls.find((call) => call.url.endsWith(JOB_OPEN.settle_endpoint));
    const settleBody = settleCall?.init?.body;
    if (typeof settleBody !== "string") throw new TypeError("expected JSON settle body");
    expect(JSON.parse(settleBody)).toEqual({
      actual_units: 7,
      broker_job_id: "broker-job-1",
      work_unit: "token",
      settlement: signedSettlement,
    });
  });

  it("submitJob selects multipart with a pre-encoded body", async () => {
    const multipartJob = { ...JOB_OPEN, transport: "multipart" as const };
    const { fetch, calls } = makeFetch({
      "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle": () => jsonResp(settledFor(2)),
      "/v1/jobs": () => jsonResp(multipartJob, { status: 201 }),
      "/v1/job": () =>
        new Response(JSON.stringify({ ok: true }), {
          headers: { "Content-Type": "application/json", ...paidJobHeaders(2) },
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await client.submitJob({
      capability: "x",
      offering: "x",
      estimatedUnits: 2,
      body: new TextEncoder().encode("--boundary--"),
      transport: "multipart",
      contentType: "multipart/form-data; boundary=boundary",
    });
    const openBody = calls[0]?.init?.body;
    if (typeof openBody !== "string") throw new TypeError("expected JSON open body");
    expect(JSON.parse(openBody).transport).toBe("multipart");
    const brokerCall = calls.find((call) => call.url.endsWith("/v1/job"));
    expect((brokerCall?.init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "multipart/form-data; boundary=boundary",
    );
  });

  it("submitJob rejects work-unit drift without settling", async () => {
    const { fetch, calls } = makeFetch({
      "/v1/jobs": () => jsonResp(JOB_OPEN, { status: 201 }),
      "/v1/job": () =>
        new Response(JSON.stringify({ ok: true }), {
          headers: {
            "Content-Type": "application/json",
            ...paidJobHeaders(3),
            "Livepeer-Work-Unit": "frames",
          },
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await expect(
      client.submitJob({
        capability: "x",
        offering: "x",
        estimatedUnits: 3,
        body: {},
      }),
    ).rejects.toBeInstanceOf(BrokerProtocolError);
    expect(calls.some((call) => call.url.endsWith(JOB_OPEN.settle_endpoint))).toBe(false);
  });

  it("submitJob forwards Livepeer-* headers to the broker", async () => {
    let brokerHeaders: Record<string, string> | undefined;
    const { fetch } = makeFetch({
      "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle": () => jsonResp(settledFor(10)),
      "/v1/jobs": () => jsonResp(JOB_OPEN, { status: 201 }),
      "/v1/job": ({ init }) => {
        brokerHeaders = (init?.headers ?? {}) as Record<string, string>;
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            ...paidJobHeaders(10),
          },
        });
      },
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await client.submitJob({
      capability: "openai:chat-completions",
      offering: "gpt-oss-20b",
      estimatedUnits: 10,
      body: { x: 1 },
      requestId: "req-zzz",
    });
    expect(brokerHeaders?.["Livepeer-Capability"]).toBe("openai:chat-completions");
    expect(brokerHeaders?.["Livepeer-Offering"]).toBe("gpt-oss-20b");
    expect(brokerHeaders?.["Livepeer-Payment"]).toBe("BASE64ENV");
    expect(brokerHeaders?.["Livepeer-Protocol"]).toBe("paid-job/v1");
    expect(brokerHeaders?.["Livepeer-Mode"]).toBeUndefined();
    expect(brokerHeaders?.["Livepeer-Spec-Version"]).toBeUndefined();
    expect(brokerHeaders?.["Livepeer-Request-Id"]).toBe("broker-request-1");
  });

  it("submitJob maps insufficient_credit error", async () => {
    const { fetch } = makeFetch({
      "/v1/jobs": () =>
        new Response(
          JSON.stringify({
            error: {
              code: "INSUFFICIENT_CREDIT",
              message: "broke",
              details: { available_wei: "0", required_wei: "1000" },
            },
          }),
          { status: 402, headers: { "Content-Type": "application/json" } },
        ),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await expect(
      client.submitJob({
        capability: "x",
        offering: "x",
        estimatedUnits: 1,
        body: {},
      }),
    ).rejects.toBeInstanceOf(InsufficientCredit);
  });

  it("submitJob maps no_route_available error", async () => {
    const { fetch } = makeFetch({
      "/v1/jobs": () =>
        new Response(
          JSON.stringify({ error: { code: "NO_ROUTE_AVAILABLE", message: "no orch" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await expect(
      client.submitJob({
        capability: "x",
        offering: "x",
        estimatedUnits: 1,
        body: {},
      }),
    ).rejects.toBeInstanceOf(NoRouteAvailable);
  });

  it("submitJob surfaces broker 4xx in JobResult.status (no raise)", async () => {
    const { fetch } = makeFetch({
      "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle": () => jsonResp(settledFor(0)),
      "/v1/jobs": () => jsonResp(JOB_OPEN, { status: 201 }),
      "/v1/job": () =>
        new Response(JSON.stringify({ error: "rate_limited" }), {
          status: 429,
          headers: {
            "Content-Type": "application/json",
            ...paidJobHeaders(0),
          },
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const result = await client.submitJob({
      capability: "x",
      offering: "x",
      estimatedUnits: 1,
      body: {},
    });
    expect(result.status).toBe(429);
    expect(result.actualUnits).toBe(0);
  });

  it("openSession returns a SessionHandle", async () => {
    const sid = "11111111-1111-1111-1111-111111111111";
    const { fetch } = makeFetch({
      "/v1/sessions": () =>
        jsonResp(
          {
            session_id: sid,
            work_id: "wid-sess",
            broker_url: BROKER,
            request_id: "req-session",
            protocol: "paid-session/v1",
            session: {
              descriptor_schema: "livepeer-session-test/v1",
              attachment: "direct",
              metering: "broker",
              refill: "extensible",
            },
            payment_envelope: "BASE64SESS",
            expected_value_wei: 100_000,
            funded_value_wei: 200_000,
            refill_endpoint: `/v1/sessions/${sid}/refill`,
            close_endpoint: `/v1/sessions/${sid}/close`,
            opened_at: "2026-05-24T12:00:00Z",
          },
          { status: 201 },
        ),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const handle = await client.openSession({
      capability: "livepeer:vtuber-session",
      offering: "vtuber-1080p30",
      descriptorSchema: "livepeer-session-test/v1",
      estimatedRunwayUnits: 100,
      maxTotalUnits: 200,
    });
    expect(handle.sessionId).toBe(sid);
    expect(handle.brokerUrl).toBe(BROKER);
    expect(handle.fundedValueWei).toBe(200_000n);
  });

  it("closeSession threads outcome", async () => {
    const sid = "22222222-2222-2222-2222-222222222222";
    let captured: unknown;
    const { fetch } = makeFetch({
      [`/v1/sessions/${sid}/close`]: ({ init }) => {
        captured = JSON.parse(init?.body as string);
        return jsonResp({
          session_id: sid,
          work_id: "w",
          actual_units: 100,
          billed_value_wei: 100_000,
          refund_wei: 0,
          outcome: "EXACT",
          closed_at: "2026-05-24T12:30:00Z",
        });
      },
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    await client.closeSession(sid, {
      actualUnits: 100,
      outcome: "EXACT",
      settlement: { payload: {}, signature: {} },
    });
    expect(captured).toEqual({
      actual_units: 100,
      outcome: "EXACT",
      settlement: { payload: {}, signature: {} },
    });
  });

  it("getSessionStatus round-trips", async () => {
    const sid = "33333333-3333-3333-3333-333333333333";
    const { fetch } = makeFetch({
      [`/v1/sessions/${sid}`]: () =>
        jsonResp({
          session_id: sid,
          work_id: "w",
          capability: "c",
          offering: "o",
          protocol: "paid-session/v1",
          state: "open",
          estimated_units: 100,
          max_total_units: 1000,
          funded_value_wei: 1_000_000,
          billed_value_wei: 100_000,
          refill_count: 0,
          cap_status: null,
          opened_at: "2026-05-24T12:00:00Z",
          closed_at: null,
          actual_units: null,
          outcome: null,
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const status = (await client.getSessionStatus(sid)) as { state: string };
    expect(status.state).toBe("open");
  });

  it("listCapabilities unwraps items", async () => {
    const { fetch } = makeFetch({
      "/v1/capabilities": () =>
        jsonResp({
          items: [{ name: "openai:embeddings", work_unit: "token", offerings: [] }],
        }),
    });
    const client = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const caps = await client.listCapabilities();
    expect(caps[0]?.name).toBe("openai:embeddings");
  });
});
