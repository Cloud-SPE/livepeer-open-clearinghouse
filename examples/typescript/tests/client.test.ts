import { describe, expect, it, vi } from "vitest";
import {
  InsufficientCredit,
  NoRouteAvailable,
  PymtHouseClient,
  RateLimited,
} from "../src/index.js";

const BASE = "http://test.local";
const KEY = "pymth_live_test_key_value";

type FetchInput = Parameters<typeof fetch>[0];

function mockFetch(impl: (req: Request) => Promise<Response> | Response): typeof fetch {
  return vi.fn(async (input: FetchInput, init?: RequestInit) => {
    const req = new Request(input as Request | string, init);
    return impl(req);
  }) as unknown as typeof fetch;
}

describe("PymtHouseClient", () => {
  it("mints on the happy path", async () => {
    const ph = new PymtHouseClient({
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
    const ph = new PymtHouseClient({
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
    const ph = new PymtHouseClient({
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
    const ph = new PymtHouseClient({
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
    const ph = new PymtHouseClient({
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
    expect(
      () => new PymtHouseClient({ baseUrl: BASE, apiKey: "not-a-real-key" }),
    ).toThrow(/pymth_/);
  });
});
