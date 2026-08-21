/**
 * SDK-side telemetry emitter.
 *
 * Mirrors the Python reference (`livepeer_open_clearinghouse_sdk.telemetry`):
 * fire-and-forget, batched, flush-on-critical, bounded buffer with
 * oldest-dropped + WARN log on overflow, gzip > 1 KiB, 3-attempt
 * exponential backoff, no `telemetry=false` opt-out.
 *
 * Per the v1 contract, every official SDK emits the same event types
 * with the same universal fields. See exec-plan 002 §"SDK telemetry".
 */

import { gzipSync } from "node:zlib";

export const DEFAULT_BATCH_SIZE = 100;
export const DEFAULT_FLUSH_INTERVAL_MS = 5000;
export const DEFAULT_BUFFER_CAP = 10_000;
export const DEFAULT_RETRIES = 3;
export const DEFAULT_GZIP_THRESHOLD_BYTES = 1024;

// Events that bypass the batch timer. `*.error` is matched by suffix
// rather than membership.
export const CRITICAL_EVENT_TYPES: ReadonlySet<string> = new Set([
  "session.refill_denied",
  "session.closed",
]);

export function isCriticalEvent(eventType: string): boolean {
  return CRITICAL_EVENT_TYPES.has(eventType) || eventType.endsWith(".error");
}

export interface TelemetryEmitOptions {
  eventType: string;
  eventSchemaVersion?: number;
  correlationId?: string | null;
  payload?: Record<string, unknown>;
  clientTs?: string;
}

interface BufferedEvent {
  event_type: string;
  event_schema_version: number;
  correlation_id: string | null;
  client_ts: string;
  payload: Record<string, unknown>;
}

export interface TelemetryEmitterOptions {
  /** Pre-built fetch-style transport. Required; the emitter doesn't
   * manage HTTP-client lifecycle. */
  fetch: typeof fetch;
  baseUrl: string;
  apiKey: string;
  sdkIdentity: string;
  endpoint?: string;
  batchSize?: number;
  flushIntervalMs?: number;
  bufferCap?: number;
  maxRetries?: number;
  gzipThresholdBytes?: number;
}

/**
 * Owned by `OpenClearinghouseClient`. Construct via the client; do
 * not instantiate directly. Call `close()` to drain remaining events
 * with one final best-effort flush.
 */
export class TelemetryEmitter {
  private readonly fetchImpl: typeof fetch;
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly sdkIdentity: string;
  private readonly endpoint: string;
  private readonly batchSize: number;
  private readonly flushIntervalMs: number;
  private readonly bufferCap: number;
  private readonly maxRetries: number;
  private readonly gzipThresholdBytes: number;

  private readonly buffer: BufferedEvent[] = [];
  private droppedCount = 0;
  private closed = false;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private inflightFlush: Promise<void> | null = null;

  constructor(opts: TelemetryEmitterOptions) {
    this.fetchImpl = opts.fetch;
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.sdkIdentity = opts.sdkIdentity;
    this.endpoint = opts.endpoint ?? "/v1/telemetry";
    this.batchSize = opts.batchSize ?? DEFAULT_BATCH_SIZE;
    this.flushIntervalMs = opts.flushIntervalMs ?? DEFAULT_FLUSH_INTERVAL_MS;
    this.bufferCap = opts.bufferCap ?? DEFAULT_BUFFER_CAP;
    this.maxRetries = opts.maxRetries ?? DEFAULT_RETRIES;
    this.gzipThresholdBytes = opts.gzipThresholdBytes ?? DEFAULT_GZIP_THRESHOLD_BYTES;
  }

  emit(opts: TelemetryEmitOptions): void {
    if (this.closed) {
      return;
    }
    const event: BufferedEvent = {
      event_type: opts.eventType,
      event_schema_version: opts.eventSchemaVersion ?? 1,
      correlation_id: opts.correlationId ?? null,
      client_ts: opts.clientTs ?? new Date().toISOString(),
      payload: opts.payload ?? {},
    };
    if (this.buffer.length === this.bufferCap) {
      this.buffer.shift();
      this.droppedCount += 1;

      console.warn(
        `[telemetry] buffer full; dropped oldest event (total dropped=${String(this.droppedCount)})`,
      );
    }
    this.buffer.push(event);
    if (isCriticalEvent(opts.eventType) || this.buffer.length >= this.batchSize) {
      void this.flush();
    } else {
      this.scheduleTimer();
    }
  }

  get bufferSize(): number {
    return this.buffer.length;
  }

  get dropped(): number {
    return this.droppedCount;
  }

  async close(): Promise<void> {
    if (this.closed) {
      return;
    }
    this.closed = true;
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (this.inflightFlush) {
      await this.inflightFlush.catch(() => undefined);
    }
    if (this.buffer.length > 0) {
      await this.flush();
    }
  }

  private scheduleTimer(): void {
    if (this.flushTimer !== null || this.closed) {
      return;
    }
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      void this.flush();
    }, this.flushIntervalMs);
    const timer = this.flushTimer as { unref?: () => void };
    if (typeof timer.unref === "function") {
      timer.unref();
    }
  }

  async flush(): Promise<void> {
    if (this.inflightFlush) {
      return this.inflightFlush;
    }
    const work = this.runFlush();
    this.inflightFlush = work;
    try {
      await work;
    } finally {
      this.inflightFlush = null;
    }
  }

  private async runFlush(): Promise<void> {
    if (this.buffer.length === 0) {
      return;
    }
    const batch = this.buffer.splice(0, this.buffer.length);
    const jsonStr = JSON.stringify({ events: batch });
    let body: string | Buffer = jsonStr;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-API-Key": this.apiKey,
      "Livepeer-Open-Clearinghouse-SDK": this.sdkIdentity,
    };
    const bodyBytes = Buffer.byteLength(jsonStr, "utf8");
    if (bodyBytes > this.gzipThresholdBytes) {
      body = gzipSync(Buffer.from(jsonStr, "utf8"));
      headers["Content-Encoding"] = "gzip";
    }
    await this.sendWithRetry(body, headers, batch.length);
  }

  private async sendWithRetry(
    body: string | Buffer,
    headers: Record<string, string>,
    eventCount: number,
  ): Promise<void> {
    let backoff = 500;
    for (let attempt = 1; attempt <= this.maxRetries; attempt += 1) {
      try {
        const res = await this.fetchImpl(`${this.baseUrl}${this.endpoint}`, {
          method: "POST",
          headers,
          body,
        });
        if (res.status < 500 && res.status !== 429) {
          return;
        }
      } catch {
        // network failure — retry
      }
      if (attempt < this.maxRetries) {
        await new Promise((r) => setTimeout(r, backoff));
        backoff *= 2;
      }
    }

    console.warn(`[telemetry] flush dropped ${String(eventCount)} events after retries`);
  }
}
