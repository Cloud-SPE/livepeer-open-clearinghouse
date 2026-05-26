import { fromResponse } from "./errors.js";
import { TelemetryEmitter } from "./telemetry.js";
import type { components } from "./_generated/openapi.js";

// ---- Generated-from-OpenAPI types ----------------------------------------
//
// The gateway's OpenAPI document at /openapi.json is the source of truth
// for response shapes. We re-export the relevant schemas under the same
// names the SDK has always exposed, so consumers don't see the import
// path leaking out. Regenerate with `make refresh-openapi` (from the repo
// root) followed by `pnpm gen:openapi` (from this directory).

export type Capability = components["schemas"]["CapabilityView"];
export type Offering = components["schemas"]["OfferingView"];
export type Orchestrator = components["schemas"]["OrchestratorView"];
export type RouteView = components["schemas"]["RouteView"];

// ---- Handoff-mode types --------------------------------------------------

export interface CapStatus {
  session_pct_used: number;
  spend_period_pct_used: number | null;
  user_balance_pct_used: number | null;
  operator_pool_pct_used: number | null;
  will_refuse_next_refill: boolean;
  winddown_reason: string | null;
}

export interface JobResult {
  body: unknown;
  status: number;
  jobId: string;
  workId: string;
  actualUnits: number;
  billedValueWei: bigint;
  refundWei: bigint;
  outcome: string;
  capStatus: CapStatus;
  requestId: string;
  rawHeaders: Record<string, string>;
}

export interface SessionHandle {
  sessionId: string;
  workId: string;
  brokerUrl: string;
  mode: string;
  paymentEnvelope: string;
  expectedValueWei: bigint;
  fundedValueWei: bigint;
  refillEndpoint: string;
  closeEndpoint: string;
}

// ---- SDK identity --------------------------------------------------------

export const SDK_LANG = "typescript";
export const SDK_VERSION = "1.3.3";
export const SDK_GIT_SHA = "dev";
export const SDK_IDENTITY = `${SDK_LANG}/${SDK_VERSION}/${SDK_GIT_SHA}`;

// ---- Client --------------------------------------------------------------

export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  /** Pass a custom fetch — defaults to global fetch. Useful in tests. */
  fetch?: typeof fetch;
  /** Per-request timeout, ms. Default 15s. */
  timeoutMs?: number;
  /** Override the SDK identity header value. */
  sdkIdentity?: string;
}

export class OpenClearinghouseClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly sdkIdentity: string;
  private readonly _telemetry: TelemetryEmitter;
  private telemetryInitDone = false;

  constructor(opts: ClientOptions) {
    if (!opts.apiKey.startsWith("pymth_")) {
      throw new Error("apiKey looks wrong (expected to start with pymth_)");
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.fetchImpl = opts.fetch ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 15_000;
    this.sdkIdentity = opts.sdkIdentity ?? SDK_IDENTITY;
    // Telemetry is mandatory — exec-plan 002 §"SDK telemetry (v1)" has
    // no opt-out. Customers route to operator-side ingest filtering
    // for any allow-list / quiet-list behavior.
    this._telemetry = new TelemetryEmitter({
      fetch: this.fetchImpl,
      baseUrl: this.baseUrl,
      apiKey: this.apiKey,
      sdkIdentity: this.sdkIdentity,
    });
  }

  /** Direct access for advanced cases (e.g. customer-side emits). */
  get telemetry(): TelemetryEmitter {
    return this._telemetry;
  }

  /** Drain the telemetry buffer with one final flush. Idempotent. */
  async close(): Promise<void> {
    await this._telemetry.close();
  }

  private emitSdkInitOnce(): void {
    if (this.telemetryInitDone) {
      return;
    }
    this.telemetryInitDone = true;
    this._telemetry.emit({
      eventType: "sdk.init",
      payload: {
        lang: SDK_LANG,
        semver: SDK_VERSION,
        git_sha7: SDK_GIT_SHA,
        runtime_version: `node/${process.version.replace(/^v/, "")}`,
        os: process.platform,
        os_version: process.arch,
        process_id: process.pid,
      },
    });
  }

  // ---- discovery ----

  async listCapabilities(): Promise<Capability[]> {
    const { items } = await this.request<{ items: Capability[] }>("GET", "/v1/capabilities");
    return items;
  }

  async listOrchestrators(opts?: { capability?: string }): Promise<Orchestrator[]> {
    const qs = opts?.capability
      ? `?capability=${encodeURIComponent(opts.capability)}`
      : "";
    const { items } = await this.request<{ items: Orchestrator[] }>(
      "GET",
      `/v1/orchestrators${qs}`,
    );
    return items;
  }

  // ---- jobs (cases a/b/c) ----

  /**
   * One-shot mint → broker call → settle for cases (a)/(b)/(c).
   *
   * Composes `POST /v1/jobs` (mint), the broker's `POST /v1/cap` with the
   * minted envelope, then `POST /v1/jobs/{id}/settle` reading
   * `Livepeer-Work-Units` from the broker's response.
   *
   * `estimatedUnits` is the SDK's best guess; `maxTotalUnits` is the
   * worst-case ceiling LOC encumbers up front (defaults to
   * `estimatedUnits` for case (a)).
   *
   * Broker-level non-2xx is returned in JobResult.status, not raised —
   * only LOC-side errors raise OpenClearinghouseError.
   */
  async submitJob(args: {
    capability: string;
    offering: string;
    estimatedUnits: number;
    body: unknown;
    maxTotalUnits?: number;
    requestId?: string;
    specVersion?: string;
    timeoutMs?: number;
  }): Promise<JobResult> {
    const requestId = args.requestId ?? crypto.randomUUID();

    this.emitSdkInitOnce();
    this._telemetry.emit({
      eventType: "request.mint_started",
      correlationId: requestId,
      payload: {
        capability: args.capability,
        offering: args.offering,
        estimated_units: args.estimatedUnits,
      },
    });
    const mintStartedNs = process.hrtime.bigint();

    // 1. Open the job
    let job: {
      job_id: string;
      work_id: string;
      broker_url: string;
      mode: string;
      payment_envelope: string;
      expected_value_wei: number;
      funded_value_wei: number;
      settle_endpoint: string;
      opened_at: string;
    };
    try {
      job = await this.request<typeof job>("POST", "/v1/jobs", {
        capability: args.capability,
        offering: args.offering,
        estimated_units: args.estimatedUnits,
        max_total_units: args.maxTotalUnits ?? null,
      });
    } catch (exc) {
      this._telemetry.emit({
        eventType: "request.error",
        correlationId: requestId,
        payload: {
          phase: "mint",
          error_class: (exc as Error)?.name ?? "unknown",
          error_code: (exc as { code?: string })?.code ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "request.mint_completed",
      correlationId: requestId,
      payload: {
        latency_ms: Number(
          (process.hrtime.bigint() - mintStartedNs) / 1_000_000n,
        ),
        funded_value_wei: job.funded_value_wei,
        mode: job.mode,
      },
    });

    // 2. Call the broker directly with the minted envelope
    let payload: string | Uint8Array;
    const baseHeaders: Record<string, string> = {
      "Livepeer-Capability": args.capability,
      "Livepeer-Offering": args.offering,
      "Livepeer-Payment": job.payment_envelope,
      "Livepeer-Mode": job.mode,
      "Livepeer-Spec-Version": args.specVersion ?? "0.1",
      "Livepeer-Request-Id": requestId,
    };
    if (args.body instanceof Uint8Array) {
      payload = args.body;
      baseHeaders["Content-Type"] = "application/octet-stream";
    } else if (typeof args.body === "string") {
      payload = args.body;
      baseHeaders["Content-Type"] = "application/octet-stream";
    } else {
      payload = JSON.stringify(args.body);
      baseHeaders["Content-Type"] = "application/json";
    }

    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      args.timeoutMs ?? 60_000,
    );
    let res: Response;
    let bodyText: string;
    try {
      res = await this.fetchImpl(`${job.broker_url.replace(/\/+$/, "")}/v1/cap`, {
        method: "POST",
        headers: baseHeaders,
        body: payload,
        signal: controller.signal,
      });
      bodyText = await res.text();
    } finally {
      clearTimeout(timeout);
    }

    // 3. Read Livepeer-Work-Units from the broker response.
    // For http-reqresp / http-multipart this is a header. For
    // http-stream it's an HTTP/1.1 chunked trailer; standard fetch
    // (browser + Node) does not expose trailers via res.headers.
    // Until WhatWG fetch grows trailer access, the http-stream
    // wire falls back to header-only detection; missing trailer
    // settles with actualUnits=0 and the LOC janitor reconciles
    // via daemon GetSessionDebits.
    const workUnitsStr = res.headers.get("livepeer-work-units");
    const actualUnits = workUnitsStr ? Number.parseInt(workUnitsStr, 10) : 0;

    // 4. Settle. Best-effort — if this fails, LOC's janitor catches it.
    this._telemetry.emit({
      eventType: "request.settle_started",
      correlationId: requestId,
    });
    const settleStartedNs = process.hrtime.bigint();
    let settled: {
      job_id: string;
      work_id: string;
      actual_units: number;
      billed_value_wei: number;
      refund_wei: number;
      outcome: string;
      closed_at: string;
      cap_status: CapStatus;
    };
    try {
      settled = await this.requestWithRetry<typeof settled>(
        "POST",
        `/v1/jobs/${job.job_id}/settle`,
        {
          actual_units: actualUnits,
        },
      );
    } catch (exc) {
      this._telemetry.emit({
        eventType: "request.error",
        correlationId: requestId,
        payload: {
          phase: "settle",
          error_class: (exc as Error)?.name ?? "unknown",
          error_code: (exc as { code?: string })?.code ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "request.settle_completed",
      correlationId: requestId,
      payload: {
        latency_ms: Number(
          (process.hrtime.bigint() - settleStartedNs) / 1_000_000n,
        ),
        refund_wei: settled.refund_wei,
        billed_value_wei: settled.billed_value_wei,
        outcome: settled.outcome,
      },
    });
    this._telemetry.emit({
      eventType: "request.completed",
      correlationId: requestId,
      payload: {
        capability: args.capability,
        offering: args.offering,
        mode: job.mode,
        estimated_units: args.estimatedUnits,
        actual_units: settled.actual_units,
        billed_value_wei: settled.billed_value_wei,
        refund_wei: settled.refund_wei,
        outcome: settled.outcome,
        broker_url: job.broker_url,
      },
    });

    // Parse body
    const ctype = res.headers.get("content-type") ?? "";
    let parsed: unknown = bodyText;
    if (ctype.includes("json") && bodyText) {
      try {
        parsed = JSON.parse(bodyText);
      } catch {
        // leave as text
      }
    }
    const headerObj: Record<string, string> = {};
    res.headers.forEach((v, k) => {
      headerObj[k] = v;
    });

    return {
      body: parsed,
      status: res.status,
      jobId: settled.job_id,
      workId: settled.work_id,
      actualUnits: settled.actual_units,
      billedValueWei: BigInt(settled.billed_value_wei),
      refundWei: BigInt(settled.refund_wei),
      outcome: settled.outcome,
      capStatus: settled.cap_status,
      requestId,
      rawHeaders: headerObj,
    };
  }

  // ---- sessions (case d) ----

  /**
   * Open a long-running session and return a `SessionHandle`.
   *
   * `maxTotalUnits` is the same input across all case-(d) modes, but
   * the operational guarantee differs by mode class:
   *
   * **(d-bounded) modes** (`ws-realtime@v0`):
   *   The session spends AT MOST `maxTotalUnits`. It may end earlier;
   *   it ends no later than when this much is consumed. It cannot be
   *   extended — refills are not supported in these modes.
   *
   * **(d-extensible) modes** (`session-control-plus-media@v0`,
   * `rtmp-ingress-hls-egress@v0`, `live-session-remote-runner@v0`,
   * `live-session-gateway-ingest@v0`):
   *   The session spends AT MOST `maxTotalUnits`. Refills happen
   *   automatically within this ceiling; the session drains if a
   *   higher-tier cap (spend-period, operator-pool) is reached
   *   before `maxTotalUnits` is exhausted.
   *
   * `estimatedRunwayUnits` is the initial chunk LOC mints toward;
   * `SessionRunner` tops up automatically as the broker signals
   * balance-low.
   */
  async openSession(args: {
    capability: string;
    offering: string;
    estimatedRunwayUnits: number;
    maxTotalUnits: number;
  }): Promise<SessionHandle> {
    this.emitSdkInitOnce();
    const data = await this.request<{
      session_id: string;
      work_id: string;
      broker_url: string;
      mode: string;
      payment_envelope: string;
      expected_value_wei: number;
      funded_value_wei: number;
      refill_endpoint: string;
      close_endpoint: string;
      opened_at: string;
    }>("POST", "/v1/sessions", {
      capability: args.capability,
      offering: args.offering,
      estimated_runway_units: args.estimatedRunwayUnits,
      max_total_units: args.maxTotalUnits,
    });
    this._telemetry.emit({
      eventType: "session.opened",
      correlationId: data.session_id,
      payload: {
        capability: args.capability,
        offering: args.offering,
        mode: data.mode,
        max_total_units: args.maxTotalUnits,
        initial_runway_units: args.estimatedRunwayUnits,
      },
    });
    return {
      sessionId: data.session_id,
      workId: data.work_id,
      brokerUrl: data.broker_url,
      mode: data.mode,
      paymentEnvelope: data.payment_envelope,
      expectedValueWei: BigInt(data.expected_value_wei),
      fundedValueWei: BigInt(data.funded_value_wei),
      refillEndpoint: data.refill_endpoint,
      closeEndpoint: data.close_endpoint,
    };
  }

  async refillSession(
    sessionId: string,
    opts: { observedConsumedUnits?: number } = {},
  ): Promise<unknown> {
    this._telemetry.emit({
      eventType: "session.refill_requested",
      correlationId: sessionId,
    });
    const refillStartedNs = process.hrtime.bigint();
    let result: Record<string, unknown>;
    try {
      result = (await this.request<Record<string, unknown>>(
        "POST",
        `/v1/sessions/${sessionId}/refill`,
        { observed_consumed_units: opts.observedConsumedUnits ?? null },
      )) as Record<string, unknown>;
    } catch (exc) {
      const status = (exc as { status?: number })?.status;
      if (status === 402) {
        const details = (exc as { details?: Record<string, unknown> })?.details ?? {};
        this._telemetry.emit({
          eventType: "session.refill_denied",
          correlationId: sessionId,
          payload: {
            which: details["which"] ?? null,
            remaining_wei: details["remaining_wei"] ?? null,
          },
        });
      } else {
        this._telemetry.emit({
          eventType: "session.error",
          correlationId: sessionId,
          payload: {
            phase: "refill",
            error_class: (exc as Error)?.name ?? "unknown",
            error_code: (exc as { code?: string })?.code ?? null,
          },
        });
      }
      throw exc;
    }
    this._telemetry.emit({
      eventType: "session.refill_granted",
      correlationId: sessionId,
      payload: {
        latency_ms: Number(
          (process.hrtime.bigint() - refillStartedNs) / 1_000_000n,
        ),
        refill_seq: result["refill_seq"] ?? null,
        funded_value_wei: result["funded_value_wei"] ?? null,
        cap_status: result["cap_status"] ?? null,
      },
    });
    return result;
  }

  async closeSession(
    sessionId: string,
    args: { actualUnits: number; outcome?: string; settlement?: unknown },
  ): Promise<unknown> {
    const body: Record<string, unknown> = { actual_units: args.actualUnits };
    if (args.outcome !== undefined) body.outcome = args.outcome;
    if (args.settlement !== undefined) body.settlement = args.settlement;
    let result: Record<string, unknown>;
    try {
      result = (await this.request<Record<string, unknown>>(
        "POST",
        `/v1/sessions/${sessionId}/close`,
        body,
      )) as Record<string, unknown>;
    } catch (exc) {
      this._telemetry.emit({
        eventType: "session.error",
        correlationId: sessionId,
        payload: {
          phase: "close",
          error_class: (exc as Error)?.name ?? "unknown",
          error_code: (exc as { code?: string })?.code ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "session.closed",
      correlationId: sessionId,
      payload: {
        actual_units: Number(result["actual_units"] ?? 0),
        billed_value_wei: Number(result["billed_value_wei"] ?? 0),
        refund_wei: Number(result["refund_wei"] ?? 0),
        outcome: result["outcome"] ?? null,
        closed_by: "customer",
      },
    });
    return result;
  }

  async getSessionStatus(sessionId: string): Promise<unknown> {
    return this.request("GET", `/v1/sessions/${sessionId}`);
  }

  // ---- internals ----

  private async requestWithRetry<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
    maxRetries = 3,
  ): Promise<T> {
    // Retry on transient failures (5xx, 429, network errors). 4xx
    // bubbles up immediately — those won't change on retry.
    let backoff = 500;
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= maxRetries; attempt += 1) {
      try {
        return await this.request<T>(method, path, body);
      } catch (e) {
        lastError = e;
        const status = (e as { status?: number })?.status ?? 0;
        if (status > 0 && status < 500 && status !== 429) {
          throw e; // client error — give up
        }
        if (attempt >= maxRetries) throw e;
        await new Promise((r) => setTimeout(r, backoff));
        backoff *= 2;
      }
    }
    throw lastError;
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      const init: RequestInit = {
        method,
        headers: {
          "X-API-Key": this.apiKey,
          "Livepeer-Open-Clearinghouse-SDK": this.sdkIdentity,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        signal: controller.signal,
      };
      if (body !== undefined) {
        init.body = JSON.stringify(body);
      }
      res = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    } finally {
      clearTimeout(timeout);
    }
    if (!res.ok) {
      let parsed: unknown = {};
      try {
        parsed = await res.json();
      } catch {
        parsed = { detail: await res.text() };
      }
      const retryAfter = res.headers.get("retry-after");
      throw fromResponse({
        status: res.status,
        body: typeof parsed === "object" && parsed !== null ? parsed : { detail: String(parsed) },
        retryAfter: retryAfter ? Number.parseInt(retryAfter, 10) : null,
      });
    }
    return (await res.json()) as T;
  }
}
