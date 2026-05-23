import { describe, expect, it, vi } from "vitest";
import {
  InsufficientCredit,
  NoRouteAvailable,
  OpenClearinghouseClient,
  RateLimited,
} from "../src/index.js";

const BASE = "http://test.local";
const KEY = "pymth_live_test_key_value";

type FetchInput = Parameters<typeof fetch>[0];

function mockFetch(impl: (req: Request) => Promise<Response> | Response): typeof fetch {
  return vi.fn((input: FetchInput, init?: RequestInit) => {
    const req = new Request(input, init);
    return impl(req);
  }) as unknown as typeof fetch;
}

describe("OpenClearinghouseClient", () => {
  it("mints on the happy path", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(
            JSON.stringify({
              payment_id: "00000000-0000-0000-0000-000000000001",
              work_id: "deadbeefdeadbeef",
              payment_bytes: "AAAA",
              expected_value_wei: "244140",
              funded_value_wei: "25000000000",
              recipient_eth_address: "0xd003",
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          ),
      ),
    });
    const mint = await ph.mintPayment({
      capability: "openai:chat-completions",
      offering: "vllm-qwen3.6-27b-default",
      workUnits: 1000,
    });
    expect(mint.payment_bytes).toBe("AAAA");
    expect(mint.recipient_eth_address).toBe("0xd003");
  });

  it("maps INSUFFICIENT_CREDIT to a typed error", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(
            JSON.stringify({
              error: {
                code: "INSUFFICIENT_CREDIT",
                message: "Available 0 < required 1000",
                details: { available_wei: "0", required_wei: "1000" },
              },
            }),
            { status: 402, headers: { "Content-Type": "application/json" } },
          ),
      ),
    });
    await expect(
      ph.mintPayment({ capability: "x", offering: "y", workUnits: 1 }),
    ).rejects.toBeInstanceOf(InsufficientCredit);
  });

  it("maps NO_ROUTE_AVAILABLE", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(
            JSON.stringify({
              error: { code: "NO_ROUTE_AVAILABLE", message: "no route" },
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
      ),
    });
    await expect(
      ph.mintPayment({ capability: "x", offering: "y", workUnits: 1 }),
    ).rejects.toBeInstanceOf(NoRouteAvailable);
  });

  it("carries Retry-After on rate-limited responses", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(JSON.stringify({ detail: "rate_limited" }), {
            status: 429,
            headers: { "Retry-After": "12", "Content-Type": "application/json" },
          }),
      ),
    });
    await expect(
      ph.mintPayment({ capability: "x", offering: "y", workUnits: 1 }),
    ).rejects.toMatchObject({
      retryAfterSeconds: 12,
      constructor: RateLimited,
    });
  });

  it("threads Idempotency-Key header through", async () => {
    let seenIdempotencyKey: string | null = null;
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch((req) => {
        seenIdempotencyKey = req.headers.get("Idempotency-Key");
        return new Response(
          JSON.stringify({
            payment_id: "00000000-0000-0000-0000-000000000001",
            work_id: "x",
            payment_bytes: "AAAA",
            expected_value_wei: "1",
            funded_value_wei: "1",
            recipient_eth_address: "0xd003",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }),
    });
    await ph.mintPayment({
      capability: "x",
      offering: "y",
      workUnits: 1,
      idempotencyKey: "abc-123",
    });
    expect(seenIdempotencyKey).toBe("abc-123");
  });

  it("rejects obviously-wrong API keys at construction", () => {
    expect(() => new OpenClearinghouseClient({ baseUrl: BASE, apiKey: "not-a-real-key" })).toThrow(
      /pymth_/,
    );
  });

  it("listCapabilities unwraps items", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(
            JSON.stringify({
              items: [{ name: "openai:chat-completions", work_unit: "tokens", offerings: [] }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      ),
    });
    const caps = await ph.listCapabilities();
    expect(caps).toHaveLength(1);
    expect(caps[0]?.name).toBe("openai:chat-completions");
  });

  it("listOrchestrators passes capability filter", async () => {
    let seenUrl: string | undefined;
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch((req) => {
        seenUrl = req.url;
        return new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    });
    await ph.listOrchestrators({ capability: "openai:chat-completions" });
    expect(seenUrl).toContain("capability=openai%3Achat-completions");
  });

  it("reportUsage returns reconciliation result", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response(
            JSON.stringify({
              refunded_wei: "12345",
              payment_status: "settled",
              new_balance_wei: "999999",
              usage: { id: "u1", actual_work_units: 800, final_charge_wei: "20000" },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      ),
    });
    const result = await ph.reportUsage({
      paymentId: "00000000-0000-0000-0000-000000000001",
      actualWorkUnits: 800,
      idempotencyKey: "abc-123",
    });
    expect(result.refunded_wei).toBe("12345");
    expect(result.new_balance_wei).toBe("999999");
  });

  it("falls back to OpenClearinghouseError on non-JSON error body", async () => {
    const ph = new OpenClearinghouseClient({
      baseUrl: BASE,
      apiKey: KEY,
      fetch: mockFetch(
        () =>
          new Response("upstream down", {
            status: 503,
            headers: { "Content-Type": "text/plain" },
          }),
      ),
    });
    await expect(
      ph.mintPayment({ capability: "x", offering: "y", workUnits: 1 }),
    ).rejects.toMatchObject({ status: 503 });
  });

  it("submitJob retries on 401 INVALID_RECIPIENT_RAND with a fresh payment", async () => {
    let mintCalls = 0;
    let orchCalls = 0;
    const seenPayments: string[] = [];
    const fetch = mockFetch((req) => {
      const url = new URL(req.url);
      if (url.pathname === "/v1/routes") {
        return new Response(
          JSON.stringify({
            eth_address: "0xd003",
            worker_url: "https://orch.example",
            capability: "x",
            offering: "y",
            price_per_work_unit_wei: "1",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.pathname === "/v1/payments/mint") {
        mintCalls += 1;
        const bytes = mintCalls === 1 ? "FIRST" : "SECOND";
        return new Response(
          JSON.stringify({
            payment_id: `00000000-0000-0000-0000-00000000000${String(mintCalls)}`,
            work_id: "abc",
            payment_bytes: bytes,
            expected_value_wei: "1",
            funded_value_wei: "1",
            recipient_eth_address: "0xd003",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.host === "orch.example" && url.pathname === "/v1/cap") {
        orchCalls += 1;
        seenPayments.push(req.headers.get("Livepeer-Payment") ?? "");
        if (orchCalls === 1) {
          return new Response(
            JSON.stringify({
              error: {
                code: "payment_invalid",
                message: "INVALID_RECIPIENT_RAND: session rotated",
              },
            }),
            { status: 401, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ model: "Qwen", choices: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`unexpected ${url.toString()}`);
    });

    const ph = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const r = await ph.submitJob({
      capability: "x",
      offering: "y",
      workUnits: 1,
      body: { messages: [] },
    });

    expect(mintCalls).toBe(2);
    expect(orchCalls).toBe(2);
    expect(seenPayments).toEqual(["FIRST", "SECOND"]);
    expect(r.status).toBe(200);
  });

  it("submitJob does not retry on an unrelated 401", async () => {
    let mintCalls = 0;
    const fetch = mockFetch((req) => {
      const url = new URL(req.url);
      if (url.pathname === "/v1/routes") {
        return new Response(
          JSON.stringify({
            eth_address: "0xd003",
            worker_url: "https://orch.example",
            capability: "x",
            offering: "y",
            price_per_work_unit_wei: "1",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.pathname === "/v1/payments/mint") {
        mintCalls += 1;
        return new Response(
          JSON.stringify({
            payment_id: "00000000-0000-0000-0000-000000000001",
            work_id: "abc",
            payment_bytes: "AAAA",
            expected_value_wei: "1",
            funded_value_wei: "1",
            recipient_eth_address: "0xd003",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ error: { code: "bad_token", message: "expired" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    });

    const ph = new OpenClearinghouseClient({ baseUrl: BASE, apiKey: KEY, fetch });
    const r = await ph.submitJob({
      capability: "x",
      offering: "y",
      workUnits: 1,
      body: { messages: [] },
    });
    expect(mintCalls).toBe(1);
    expect(r.status).toBe(401);
  });
});
