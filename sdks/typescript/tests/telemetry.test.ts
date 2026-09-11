/**
 * Tests for the TS-side TelemetryEmitter — parity with the Python
 * reference test suite.
 */

import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";

import {
  CRITICAL_EVENT_TYPES,
  TelemetryEmitter,
  isCriticalEvent,
  telemetryCorrelationId,
} from "../src/telemetry.js";

// python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, 'loc-test-chat-abc'))"
const EXPECTED_V5 = "ab6ae49d-69c6-579d-8f36-8b7eaeed58a6";

interface CapturedRequest {
  url: string;
  init: RequestInit | undefined;
}

function makeFetch(status = 202): {
  fetch: typeof fetch;
  calls: CapturedRequest[];
} {
  const calls: CapturedRequest[] = [];
  const fetchImpl: typeof fetch = (input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    calls.push({ url, init });
    return Promise.resolve(new Response(JSON.stringify({ accepted: 1 }), { status }));
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

describe("telemetryCorrelationId", () => {
  it("passes a UUID through lowercased (any version)", () => {
    expect(telemetryCorrelationId("6BA7B811-9DAD-11D1-80B4-00C04FD430C8")).toBe(
      "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
    );
    const v4 = "d3f0b8a2-4c9e-4f1a-9b2c-7e6f5a4d3c2b";
    expect(telemetryCorrelationId(v4)).toBe(v4);
    const v7 = "018f4e9a-1b2c-7d3e-8f4a-5b6c7d8e9f0a";
    expect(telemetryCorrelationId(v7)).toBe(v7);
  });

  it("derives a stable uuid5 under NAMESPACE_URL for non-UUID strings", () => {
    expect(telemetryCorrelationId("loc-test-chat-abc")).toBe(EXPECTED_V5);
    expect(telemetryCorrelationId("loc-test-chat-abc")).toBe(EXPECTED_V5);
    expect(telemetryCorrelationId("loc-test-chat-abd")).not.toBe(EXPECTED_V5);
    expect(telemetryCorrelationId("")).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
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
    const call = calls[0];
    if (call === undefined) throw new Error("missing telemetry call");
    const headers = call.init?.headers as Record<string, string>;
    expect(headers["Content-Encoding"]).toBe("gzip");
    const decompressed = gunzipSync(Buffer.from(call.init?.body as Uint8Array)).toString("utf8");
    const parsed = JSON.parse(decompressed);
    expect(parsed.events[0].event_type).toBe("request.mint_started");
  });

  it("retries on 5xx then drops", async () => {
    const calls: CapturedRequest[] = [];
    const fetchImpl: typeof fetch = (input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      calls.push({ url, init });
      return Promise.resolve(new Response("err", { status: 503 }));
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
      correlationId: "loc-test-chat-abc",
      payload: { capability: "x" },
    });
    em.emit({
      eventType: "request.mint_completed",
      correlationId: "6BA7B811-9DAD-11D1-80B4-00C04FD430C8",
    });
    em.emit({ eventType: "request.settle_started" });
    await em.close();
    expect(calls.length).toBe(1);
    const call = calls[0];
    if (call === undefined) throw new Error("missing telemetry call");
    const body = JSON.parse(call.init?.body as string);
    const ev = body.events[0];
    expect(ev.event_type).toBe("request.mint_started");
    expect(ev.event_schema_version).toBe(1);
    // Non-UUID request ids are mapped to uuid5 so the gateway accepts them.
    expect(ev.correlation_id).toBe(EXPECTED_V5);
    expect(body.events[1].correlation_id).toBe("6ba7b811-9dad-11d1-80b4-00c04fd430c8");
    expect(body.events[2].correlation_id).toBeNull();
    expect(ev.client_ts).toBeTruthy();
    expect(ev.payload).toEqual({ capability: "x" });
  });
});
