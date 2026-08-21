/**
 * Tests for the TS-side TelemetryEmitter — parity with the Python
 * reference test suite.
 */

import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";

import { CRITICAL_EVENT_TYPES, TelemetryEmitter, isCriticalEvent } from "../src/telemetry.js";

interface CapturedRequest {
  url: string;
  init: RequestInit | undefined;
}

function makeFetch(status = 202): {
  fetch: typeof fetch;
  calls: CapturedRequest[];
} {
  const calls: CapturedRequest[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    calls.push({ url, init });
    return new Response(JSON.stringify({ accepted: 1 }), { status });
  };
  return { fetch: fetchImpl, calls };
}

describe("isCriticalEvent", () => {
  it("matches the documented set + *.error suffix", () => {
    for (const et of CRITICAL_EVENT_TYPES) {
      expect(isCriticalEvent(et)).toBe(true);
    }
    expect(isCriticalEvent("request.error")).toBe(true);
    expect(isCriticalEvent("session.error")).toBe(true);
    expect(isCriticalEvent("custom.deep.subsystem.error")).toBe(true);
    expect(isCriticalEvent("request.mint_started")).toBe(false);
    expect(isCriticalEvent("sdk.init")).toBe(false);
  });
});

describe("TelemetryEmitter buffering", () => {
  it("buffers below batch size", () => {
    const { fetch: f } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 100,
    });
    em.emit({ eventType: "request.mint_started" });
    expect(em.bufferSize).toBe(1);
  });

  it("flushes at batch size", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 3,
    });
    em.emit({ eventType: "request.mint_started" });
    em.emit({ eventType: "request.mint_completed" });
    em.emit({ eventType: "request.broker_call_started" });
    // Wait one tick for the async flush.
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.length).toBe(1);
    await em.close();
  });

  it("flushes critical events immediately", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 100,
    });
    em.emit({ eventType: "session.refill_denied" });
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.length).toBe(1);
    await em.close();
  });

  it("drops oldest on buffer overflow", () => {
    const { fetch: f } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 9_999,
      bufferCap: 3,
    });
    em.emit({ eventType: "a.event" });
    em.emit({ eventType: "b.event" });
    em.emit({ eventType: "c.event" });
    em.emit({ eventType: "d.event" });
    em.emit({ eventType: "e.event" });
    expect(em.bufferSize).toBe(3);
    expect(em.dropped).toBe(2);
  });

  it("close drains the remainder", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 999,
    });
    em.emit({ eventType: "request.mint_started" });
    expect(calls.length).toBe(0);
    await em.close();
    expect(calls.length).toBe(1);
  });

  it("emit after close is silent", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
    });
    await em.close();
    em.emit({ eventType: "post.close" });
    expect(em.bufferSize).toBe(0);
    expect(calls.length).toBe(0);
  });
});

describe("TelemetryEmitter wire", () => {
  it("applies gzip when body exceeds threshold", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 999,
      gzipThresholdBytes: 10,
    });
    em.emit({
      eventType: "request.mint_started",
      payload: { x: "y".repeat(200) },
    });
    await em.close();
    expect(calls.length).toBe(1);
    const headers = calls[0]!.init?.headers as Record<string, string>;
    expect(headers["Content-Encoding"]).toBe("gzip");
    const decompressed = gunzipSync(Buffer.from(calls[0]!.init!.body as Uint8Array)).toString(
      "utf8",
    );
    const parsed = JSON.parse(decompressed);
    expect(parsed.events[0].event_type).toBe("request.mint_started");
  });

  it("retries on 5xx then drops", async () => {
    const calls: CapturedRequest[] = [];
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      calls.push({ url, init });
      return new Response("err", { status: 503 });
    };
    const em = new TelemetryEmitter({
      fetch: fetchImpl,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 999,
      maxRetries: 3,
    });
    em.emit({ eventType: "session.refill_denied" });
    // Each retry sleeps 500ms → ~1.5s total.
    await new Promise((r) => setTimeout(r, 3000));
    expect(calls.length).toBe(3);
    await em.close();
  });

  it("event carries the universal fields", async () => {
    const { fetch: f, calls } = makeFetch();
    const em = new TelemetryEmitter({
      fetch: f,
      baseUrl: "http://loc.test",
      apiKey: "pymth_live_test",
      sdkIdentity: "typescript/0.0.1/dev",
      flushIntervalMs: 999_999,
      batchSize: 999,
    });
    em.emit({
      eventType: "request.mint_started",
      correlationId: "abc-123",
      payload: { capability: "x" },
    });
    await em.close();
    expect(calls.length).toBe(1);
    const body = JSON.parse(calls[0]!.init!.body as string);
    const ev = body.events[0];
    expect(ev.event_type).toBe("request.mint_started");
    expect(ev.event_schema_version).toBe(1);
    expect(ev.correlation_id).toBe("abc-123");
    expect(ev.client_ts).toBeTruthy();
    expect(ev.payload).toEqual({ capability: "x" });
  });
});
