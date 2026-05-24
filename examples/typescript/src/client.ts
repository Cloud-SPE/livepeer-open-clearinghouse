import { fromResponse } from "./errors.js";
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
export const SDK_VERSION = "0.2.0";
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

  constructor(opts: ClientOptions) {
    if (!opts.apiKey.startsWith("pymth_")) {
      throw new Error("apiKey looks wrong (expected to start with pymth_)");
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.fetchImpl = opts.fetch ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 15_000;
    this.sdkIdentity = opts.sdkIdentity ?? SDK_IDENTITY;
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

    // 1. Open the job
    const job = await this.request<{
      job_id: string;
      work_id: string;
      broker_url: string;
      mode: string;
      payment_envelope: string;
      expected_value_wei: number;
      funded_value_wei: number;
      settle_endpoint: string;
      opened_at: string;
    }>("POST", "/v1/jobs", {
      capability: args.capability,
      offering: args.offering,
      estimated_units: args.estimatedUnits,
      max_total_units: args.maxTotalUnits ?? null,
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

    // 3. Read Livepeer-Work-Units from the broker response
    const workUnitsStr = res.headers.get("livepeer-work-units");
    const actualUnits = workUnitsStr ? Number.parseInt(workUnitsStr, 10) : 0;

    // 4. Settle. Best-effort — if this fails, LOC's janitor catches it.
    const settled = await this.request<{
      job_id: string;
      work_id: string;
      actual_units: number;
      billed_value_wei: number;
      refund_wei: number;
      outcome: string;
      closed_at: string;
      cap_status: CapStatus;
    }>("POST", `/v1/jobs/${job.job_id}/settle`, {
      actual_units: actualUnits,
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

  async openSession(args: {
    capability: string;
    offering: string;
    estimatedRunwayUnits: number;
    maxTotalUnits: number;
  }): Promise<SessionHandle> {
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
    return this.request("POST", `/v1/sessions/${sessionId}/refill`, {
      observed_consumed_units: opts.observedConsumedUnits ?? null,
    });
  }

  async closeSession(
    sessionId: string,
    args: { actualUnits: number; outcome?: string; settlement?: unknown },
  ): Promise<unknown> {
    const body: Record<string, unknown> = { actual_units: args.actualUnits };
    if (args.outcome !== undefined) body.outcome = args.outcome;
    if (args.settlement !== undefined) body.settlement = args.settlement;
    return this.request("POST", `/v1/sessions/${sessionId}/close`, body);
  }

  async getSessionStatus(sessionId: string): Promise<unknown> {
    return this.request("GET", `/v1/sessions/${sessionId}`);
  }

  // ---- internals ----

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
