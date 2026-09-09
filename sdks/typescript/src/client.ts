import { BrokerProtocolError, fromResponse } from "./errors.js";
import { TelemetryEmitter } from "./telemetry.js";
import type { components } from "./_generated/openapi.js";

function errorClass(error: unknown): string {
  return error instanceof Error ? error.name : "unknown";
}

function errorProperty(error: unknown, key: string): unknown {
  return typeof error === "object" && error !== null && key in error
    ? (error as Record<string, unknown>)[key]
    : undefined;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !(value instanceof Uint8Array)
  );
}

export type CallerProofSigner = (authorization: Uint8Array) => string | Promise<string>;

function encodeBody(body: unknown): string | Uint8Array {
  if (body instanceof Uint8Array || typeof body === "string") return body;
  return JSON.stringify(body);
}

async function sha256Hex(body: string | Uint8Array): Promise<string> {
  const bytes = typeof body === "string" ? new TextEncoder().encode(body) : body;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}

export async function callerProof(
  authorization: string | null | undefined,
  signer: CallerProofSigner | undefined,
): Promise<string | null> {
  if (authorization == null) return null;
  if (signer === undefined) {
    throw new BrokerProtocolError(
      "LOC returned a spend authorization but no caller-proof signer was supplied",
      { code: "caller_proof_signer_required" },
    );
  }
  const proof = await signer(Buffer.from(authorization, "base64"));
  if (proof.length === 0) {
    throw new BrokerProtocolError("caller-proof signer returned an empty proof", {
      code: "caller_proof_signer_failed",
    });
  }
  return proof;
}

/**
 * Fill `model` for `openai:*` capabilities from the route LOC selected.
 *
 * Only applies when the body is a JSON object with no own `model` key
 * and the job's `route_snapshot.extra.openai.model` is a non-empty
 * string; every other body is returned as-is.
 */
function withRouteModel(
  capability: string,
  body: unknown,
  routeSnapshot: { extra?: Record<string, unknown> } | undefined,
): unknown {
  if (!capability.startsWith("openai:") || !isPlainObject(body)) {
    return body;
  }
  if (Object.prototype.hasOwnProperty.call(body, "model")) {
    return body;
  }
  const openai = routeSnapshot?.extra?.openai;
  const model = isPlainObject(openai) ? openai.model : undefined;
  if (typeof model !== "string" || model.length === 0) {
    return body;
  }
  return { ...body, model };
}

// ---- Wei parsing ---------------------------------------------------------
//
// LOC emits every `*_wei` field as a decimal integer string so values above
// 2**53 survive JSON intact. Older gateways emitted JSON numbers; those are
// still accepted as long as they are safe integers.

const CANONICAL_WEI = /^-?\d+$/;

/**
 * Parse a wei amount from an LOC response into a `bigint`.
 *
 * Accepts the canonical integer string (`"12345678901234567890"`) or a
 * legacy safe-integer JSON number. Anything else — `null`, `undefined`,
 * fractional or unsafe numbers, non-integer strings — throws a
 * `BrokerProtocolError` with code `wei_malformed`.
 */
export function parseWei(value: string | number | null | undefined): bigint {
  if (typeof value === "string") {
    if (CANONICAL_WEI.test(value)) {
      return BigInt(value);
    }
  } else if (typeof value === "number") {
    if (Number.isSafeInteger(value)) {
      return BigInt(value);
    }
  }
  throw new BrokerProtocolError("LOC returned a malformed wei amount", {
    code: "wei_malformed",
    details: { value: value === undefined ? null : value, type: typeof value },
  });
}

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
export type JobStatus = components["schemas"]["JobStatusResponse"];

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
  brokerJobId: string;
  protocol: string;
  transport: "unary" | "stream" | "multipart";
  workUnit: string;
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
  requestId: string;
  workId: string;
  brokerUrl: string;
  protocol: "paid-session/v1";
  capability: string;
  offering: string;
  session: SessionAxes;
  sessionParams: Record<string, unknown>;
  paymentEnvelope: string | null;
  spendAuthorization?: string | null;
  accountingMode?: "legacy_ticket" | "wholesale_account";
  callerProof?: string | null;
  sessionOpenBody?: string;
  maxTotalUnits?: number;
  signCallerProof?: CallerProofSigner;
  expectedValueWei: bigint;
  fundedValueWei: bigint;
  refillEndpoint: string;
  closeEndpoint: string;
}

export interface SessionAxes {
  descriptor_schema: string;
  attachment: "external";
  metering: "runner-reported";
  refill: "extensible" | "bounded";
  [key: string]: unknown;
}

// ---- SDK identity --------------------------------------------------------

export const SDK_LANG = "typescript";
export const SDK_VERSION = "2.0.0";
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
    const qs = opts?.capability ? `?capability=${encodeURIComponent(opts.capability)}` : "";
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
   * Composes `POST /v1/jobs` (mint), the broker's `POST /v1/job` with the
   * minted envelope, then `POST /v1/jobs/{id}/settle` reading
   * `Livepeer-Work-Units` from the broker's response.
   *
   * `estimatedUnits` is the SDK's best guess; `maxTotalUnits` is the
   * worst-case ceiling LOC encumbers up front (defaults to
   * `estimatedUnits` for case (a)).
   *
   * Broker-level non-2xx is returned in JobResult.status, not raised —
   * only LOC-side errors raise OpenClearinghouseError.
   *
   * For `openai:*` capabilities you don't need a `model` field in a JSON
   * `body`: when it is absent the SDK fills it from the route selected
   * by LOC (`route_snapshot.extra.openai.model`). A caller-supplied
   * `model` is always sent untouched.
   */
  async submitJob(args: {
    capability: string;
    offering: string;
    estimatedUnits: number;
    body: unknown;
    maxTotalUnits?: number;
    requestId?: string;
    transport?: "unary" | "stream" | "multipart";
    contentType?: string;
    timeoutMs?: number;
    callerPublicKey?: string;
    signCallerProof?: CallerProofSigner;
  }): Promise<JobResult> {
    const requestId = args.requestId ?? crypto.randomUUID();
    const requestedTransport = args.transport ?? "unary";
    if (
      requestedTransport === "multipart" &&
      (!(args.body instanceof Uint8Array || typeof args.body === "string") ||
        !args.contentType?.toLowerCase().startsWith("multipart/form-data"))
    ) {
      throw new TypeError(
        "multipart transport requires a pre-encoded body and multipart/form-data contentType",
      );
    }
    if ((args.callerPublicKey === undefined) !== (args.signCallerProof === undefined)) {
      throw new TypeError("callerPublicKey and signCallerProof must be supplied together");
    }
    const authorizationBody = encodeBody(args.body);

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
      request_id: string;
      work_id: string;
      broker_url: string;
      protocol: string;
      transport: "unary" | "stream" | "multipart";
      work_unit: string;
      payment_envelope?: string | null;
      spend_authorization?: string | null;
      accounting_mode?: string;
      expected_value_wei: string;
      funded_value_wei: string;
      settle_endpoint: string;
      opened_at: string;
      route_snapshot?: { extra?: Record<string, unknown> };
    };
    try {
      job = await this.request<typeof job>(
        "POST",
        "/v1/jobs",
        {
          capability: args.capability,
          offering: args.offering,
          transport: requestedTransport,
          estimated_units: args.estimatedUnits,
          max_total_units: args.maxTotalUnits ?? null,
          workload_request_digest: await sha256Hex(authorizationBody),
          caller_public_key: args.callerPublicKey ?? null,
        },
        { "Idempotency-Key": requestId },
      );
    } catch (exc) {
      this._telemetry.emit({
        eventType: "request.error",
        correlationId: requestId,
        payload: {
          phase: "mint",
          error_class: errorClass(exc),
          error_code: errorProperty(exc, "code") ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "request.mint_completed",
      correlationId: requestId,
      payload: {
        latency_ms: Number((process.hrtime.bigint() - mintStartedNs) / 1_000_000n),
        funded_value_wei: job.funded_value_wei,
        protocol: job.protocol,
      },
    });

    if (job.protocol !== "paid-job/v1") {
      throw new BrokerProtocolError(`LOC returned unsupported job protocol ${job.protocol}`, {
        code: "protocol_unsupported",
        details: { protocol: job.protocol },
      });
    }
    if (job.transport !== requestedTransport) {
      throw new BrokerProtocolError(
        `LOC returned transport ${job.transport}; requested ${requestedTransport}`,
        {
          code: "protocol_transport_mismatch",
          details: { expected: requestedTransport, received: job.transport },
        },
      );
    }

    // 2. Call the broker directly with the minted envelope
    const accountingMode = job.accounting_mode ?? "legacy_ticket";
    const brokerBody =
      accountingMode === "wholesale_account"
        ? args.body
        : withRouteModel(args.capability, args.body, job.route_snapshot);
    let payload: string | Uint8Array;
    const baseHeaders: Record<string, string> = {
      "Livepeer-Capability": args.capability,
      "Livepeer-Offering": args.offering,
      "Livepeer-Protocol": job.protocol,
      "Livepeer-Request-Id": job.request_id,
    };
    if (accountingMode === "wholesale_account") {
      const proof = await callerProof(job.spend_authorization, args.signCallerProof);
      if (job.spend_authorization == null || proof == null) {
        throw new BrokerProtocolError("LOC returned an incomplete wholesale authorization");
      }
      baseHeaders["Livepeer-Authorization"] = job.spend_authorization;
      baseHeaders["Livepeer-Caller-Proof"] = proof;
      if (job.payment_envelope != null) baseHeaders["Livepeer-Payment"] = job.payment_envelope;
    } else if (accountingMode === "legacy_ticket") {
      if (job.payment_envelope == null || job.payment_envelope.length === 0) {
        throw new BrokerProtocolError("LOC returned no payment envelope for legacy accounting");
      }
      baseHeaders["Livepeer-Payment"] = job.payment_envelope;
    } else {
      throw new BrokerProtocolError(`LOC returned unsupported accounting mode ${accountingMode}`);
    }
    if (brokerBody instanceof Uint8Array) {
      payload = brokerBody;
      baseHeaders["Content-Type"] = args.contentType ?? "application/octet-stream";
    } else if (typeof brokerBody === "string") {
      payload = brokerBody;
      baseHeaders["Content-Type"] = args.contentType ?? "application/octet-stream";
    } else {
      payload =
        accountingMode === "wholesale_account" ? authorizationBody : JSON.stringify(brokerBody);
      baseHeaders["Content-Type"] = "application/json";
    }
    if (requestedTransport === "stream") {
      baseHeaders.Accept = "text/event-stream";
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), args.timeoutMs ?? 60_000);
    let res: Response;
    let bodyText: string;
    let terminalClaim: {
      brokerJobId: string;
      brokerWorkUnit: string;
      settlementPayload: Record<string, unknown>;
    };
    try {
      const brokerBaseUrl = job.broker_url.replace(/\/+$/, "");
      res = await this.fetchImpl(`${brokerBaseUrl}/v1/job`, {
        method: "POST",
        headers: baseHeaders,
        body: payload,
        signal: controller.signal,
      });
      bodyText = await res.text();

      let claimRes = res;
      if (requestedTransport === "stream") {
        const initialJobId = res.headers.get("livepeer-job-id");
        if (!initialJobId) {
          throw new BrokerProtocolError("stream response missing Livepeer-Job-Id", {
            status: res.status,
            details: { missing_headers: ["Livepeer-Job-Id"] },
          });
        }
        const settlementUrl = `${brokerBaseUrl}/v1/settlement/${encodeURIComponent(initialJobId)}`;
        for (let attempt = 0; attempt < 4; attempt += 1) {
          const query = await this.fetchImpl(settlementUrl, {
            method: "GET",
            signal: controller.signal,
          });
          if (query.status === 202 && attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, 50 * 2 ** attempt));
            continue;
          }
          claimRes = query;
          break;
        }
        if (claimRes.headers.get("livepeer-job-id") !== initialJobId) {
          throw new BrokerProtocolError("settlement query returned a different job id", {
            code: "broker_job_id_mismatch",
            status: claimRes.status,
            details: {
              expected: initialJobId,
              received: claimRes.headers.get("livepeer-job-id"),
            },
          });
        }
      }

      const workUnitsStr = claimRes.headers.get("livepeer-work-units");
      const brokerWorkUnit = claimRes.headers.get("livepeer-work-unit");
      const brokerJobId = claimRes.headers.get("livepeer-job-id");
      const missing: string[] = [];
      if (workUnitsStr === null) missing.push("Livepeer-Work-Units");
      if (brokerWorkUnit === null) missing.push("Livepeer-Work-Unit");
      if (brokerJobId === null) missing.push("Livepeer-Job-Id");
      if (missing.length > 0) {
        throw new BrokerProtocolError(
          `terminal broker response missing required headers: ${missing.join(", ")}`,
          { status: claimRes.status, details: { missing_headers: missing } },
        );
      }
      if (workUnitsStr === null || brokerWorkUnit === null || brokerJobId === null) {
        throw new BrokerProtocolError("unreachable missing terminal broker headers");
      }
      if (brokerWorkUnit !== job.work_unit) {
        throw new BrokerProtocolError(
          `broker reported work unit ${brokerWorkUnit}; expected ${job.work_unit}`,
          {
            code: "work_unit_mismatch",
            status: claimRes.status,
            details: { expected: job.work_unit, received: brokerWorkUnit },
          },
        );
      }
      const actualUnits = Number.parseInt(workUnitsStr, 10);
      if (!Number.isSafeInteger(actualUnits) || actualUnits < 0) {
        throw new BrokerProtocolError(`invalid Livepeer-Work-Units: ${workUnitsStr}`, {
          status: claimRes.status,
        });
      }

      const settlementPayload: Record<string, unknown> = {
        actual_units: actualUnits,
        broker_job_id: brokerJobId,
        work_unit: brokerWorkUnit,
      };
      const encodedSettlement = claimRes.headers.get("livepeer-settlement");
      if (!encodedSettlement) {
        throw new BrokerProtocolError("terminal broker response missing Livepeer-Settlement", {
          status: claimRes.status,
          details: { missing_headers: ["Livepeer-Settlement"] },
        });
      }
      try {
        const bytes = Uint8Array.from(atob(encodedSettlement), (char) => char.charCodeAt(0));
        settlementPayload.settlement = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
      } catch {
        throw new BrokerProtocolError(
          "terminal broker response has malformed Livepeer-Settlement",
          {
            status: claimRes.status,
          },
        );
      }

      // 4. Settle. Retain these values outside the request block without
      // weakening them to optional types.
      terminalClaim = {
        brokerJobId,
        brokerWorkUnit,
        settlementPayload,
      };
    } finally {
      clearTimeout(timeout);
    }

    // 4. Settle.
    this._telemetry.emit({
      eventType: "request.settle_started",
      correlationId: requestId,
    });
    const settleStartedNs = process.hrtime.bigint();
    let settled: {
      job_id: string;
      work_id: string;
      actual_units: number;
      billed_value_wei: string;
      refund_wei: string;
      outcome: string;
      closed_at: string;
      cap_status: CapStatus;
    };
    try {
      settled = await this.requestWithRetry<typeof settled>(
        "POST",
        `/v1/jobs/${job.job_id}/settle`,
        terminalClaim.settlementPayload,
      );
    } catch (exc) {
      this._telemetry.emit({
        eventType: "request.error",
        correlationId: requestId,
        payload: {
          phase: "settle",
          error_class: errorClass(exc),
          error_code: errorProperty(exc, "code") ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "request.settle_completed",
      correlationId: requestId,
      payload: {
        latency_ms: Number((process.hrtime.bigint() - settleStartedNs) / 1_000_000n),
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
        protocol: job.protocol,
        transport: job.transport,
        work_unit: job.work_unit,
        broker_job_id: terminalClaim.brokerJobId,
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
      brokerJobId: terminalClaim.brokerJobId,
      protocol: job.protocol,
      transport: job.transport,
      workUnit: terminalClaim.brokerWorkUnit,
      actualUnits: settled.actual_units,
      billedValueWei: parseWei(settled.billed_value_wei),
      refundWei: parseWei(settled.refund_wei),
      outcome: settled.outcome,
      capStatus: settled.cap_status,
      requestId: job.request_id,
      rawHeaders: headerObj,
    };
  }

  // ---- sessions (case d) ----

  /**
   * Open a long-running session and return a `SessionHandle`.
   *
   * `maxTotalUnits` is a hard spend ceiling. Whether the session can extend
   * within that ceiling comes from the offering's `session.refill` axis:
   * `bounded` drains without refilling, while `extensible` uses the broker's
   * authoritative HTTP top-up contract.
   *
   * `estimatedRunwayUnits` is the initial chunk LOC mints toward;
   * `SessionRunner` tops up automatically as the broker reports a normative
   * low balance.
   */
  async openSession(args: {
    capability: string;
    offering: string;
    descriptorSchema: string;
    sessionParams?: Record<string, unknown>;
    estimatedRunwayUnits: number;
    maxTotalUnits: number;
    requestId?: string;
    callerPublicKey?: string;
    signCallerProof?: CallerProofSigner;
  }): Promise<SessionHandle> {
    if ((args.callerPublicKey === undefined) !== (args.signCallerProof === undefined)) {
      throw new TypeError("callerPublicKey and signCallerProof must be supplied together");
    }
    this.emitSdkInitOnce();
    const locRequestId = args.requestId ?? crypto.randomUUID();
    const sessionParams = args.sessionParams ?? {};
    let prepared:
      | {
          gateway_session_id: string;
          route_binding: Record<string, unknown>;
          preparation_token: string;
        }
      | undefined;
    let sessionOpenBody = "";
    if (args.callerPublicKey !== undefined) {
      const preparation = await this.request<NonNullable<typeof prepared>>(
        "POST",
        "/v1/sessions/prepare",
        {
          capability: args.capability,
          offering: args.offering,
          descriptor_schema: args.descriptorSchema,
        },
        { "Idempotency-Key": `${locRequestId}:prepare` },
      );
      prepared = preparation;
      sessionOpenBody = JSON.stringify({
        gateway_session_id: preparation.gateway_session_id,
        session_params: sessionParams,
      });
    }
    const openBody: Record<string, unknown> = {
      capability: args.capability,
      offering: args.offering,
      descriptor_schema: args.descriptorSchema,
      session_params: sessionParams,
      estimated_runway_units: args.estimatedRunwayUnits,
      max_total_units: args.maxTotalUnits,
    };
    if (prepared !== undefined) {
      Object.assign(openBody, {
        gateway_session_id: prepared.gateway_session_id,
        preparation_token: prepared.preparation_token,
        route_binding: prepared.route_binding,
        workload_request_digest: await sha256Hex(sessionOpenBody),
        caller_public_key: args.callerPublicKey,
      });
    }
    const data = await this.request<{
      session_id: string;
      request_id: string;
      work_id: string;
      broker_url: string;
      protocol: string;
      session: SessionAxes;
      payment_envelope?: string | null;
      spend_authorization?: string | null;
      accounting_mode?: string;
      expected_value_wei: string;
      funded_value_wei: string;
      refill_endpoint: string;
      close_endpoint: string;
      opened_at: string;
    }>("POST", "/v1/sessions", openBody, { "Idempotency-Key": locRequestId });
    if (data.protocol !== "paid-session/v1") {
      throw new BrokerProtocolError(`LOC returned unsupported protocol ${data.protocol}`);
    }
    if (data.session.descriptor_schema !== args.descriptorSchema) {
      throw new BrokerProtocolError("LOC returned an unexpected descriptor schema");
    }
    if (prepared === undefined) {
      sessionOpenBody = JSON.stringify({
        gateway_session_id: data.session_id,
        session_params: sessionParams,
      });
    }
    const accountingMode = data.accounting_mode ?? "legacy_ticket";
    if (accountingMode !== "legacy_ticket" && accountingMode !== "wholesale_account") {
      throw new BrokerProtocolError(`LOC returned unsupported accounting mode ${accountingMode}`);
    }
    const proof = await callerProof(data.spend_authorization, args.signCallerProof);
    if (accountingMode === "legacy_ticket" && !data.payment_envelope) {
      throw new BrokerProtocolError("LOC returned no payment envelope for legacy accounting");
    }
    if (accountingMode === "wholesale_account" && (!data.spend_authorization || proof == null)) {
      throw new BrokerProtocolError("LOC returned an incomplete wholesale authorization");
    }
    this._telemetry.emit({
      eventType: "session.opened",
      correlationId: data.session_id,
      payload: {
        capability: args.capability,
        offering: args.offering,
        protocol: data.protocol,
        descriptor_schema: data.session.descriptor_schema,
        refill: data.session.refill,
        max_total_units: args.maxTotalUnits,
        initial_runway_units: args.estimatedRunwayUnits,
      },
    });
    return {
      sessionId: data.session_id,
      requestId: data.request_id,
      workId: data.work_id,
      brokerUrl: data.broker_url,
      protocol: "paid-session/v1",
      capability: args.capability,
      offering: args.offering,
      session: data.session,
      sessionParams,
      paymentEnvelope: data.payment_envelope ?? null,
      spendAuthorization: data.spend_authorization ?? null,
      accountingMode,
      callerProof: proof,
      sessionOpenBody,
      maxTotalUnits: args.maxTotalUnits,
      ...(args.signCallerProof === undefined ? {} : { signCallerProof: args.signCallerProof }),
      expectedValueWei: parseWei(data.expected_value_wei),
      fundedValueWei: parseWei(data.funded_value_wei),
      refillEndpoint: data.refill_endpoint,
      closeEndpoint: data.close_endpoint,
    };
  }

  async refillSession(
    sessionId: string,
    opts: {
      observedConsumedUnits?: number;
      requestId?: string;
      rebindFrom?: string;
      replacesRequestId?: string;
      maxTotalUnits?: number;
      workloadRequestDigest?: string;
    } = {},
  ): Promise<unknown> {
    this._telemetry.emit({
      eventType: "session.refill_requested",
      correlationId: sessionId,
    });
    const refillStartedNs = process.hrtime.bigint();
    const locRequestId = opts.requestId ?? crypto.randomUUID();
    const body: Record<string, unknown> = {
      observed_consumed_units: opts.observedConsumedUnits ?? null,
    };
    if (opts.maxTotalUnits !== undefined) {
      body.max_total_units = opts.maxTotalUnits;
      body.workload_request_digest = opts.workloadRequestDigest;
    }
    if (opts.rebindFrom !== undefined) {
      body.rebind_from = opts.rebindFrom;
      body.replaces_request_id = opts.replacesRequestId;
    }
    let result: Record<string, unknown>;
    try {
      result = await this.request<Record<string, unknown>>(
        "POST",
        `/v1/sessions/${sessionId}/refill`,
        body,
        { "Idempotency-Key": locRequestId },
      );
    } catch (exc) {
      const status = errorProperty(exc, "status");
      if (status === 402) {
        const candidate = errorProperty(exc, "details");
        const details =
          typeof candidate === "object" && candidate !== null
            ? (candidate as Record<string, unknown>)
            : {};
        this._telemetry.emit({
          eventType: "session.refill_denied",
          correlationId: sessionId,
          payload: {
            which: details.which ?? null,
            remaining_wei: details.remaining_wei ?? null,
          },
        });
      } else {
        this._telemetry.emit({
          eventType: "session.error",
          correlationId: sessionId,
          payload: {
            phase: "refill",
            error_class: errorClass(exc),
            error_code: errorProperty(exc, "code") ?? null,
          },
        });
      }
      throw exc;
    }
    this._telemetry.emit({
      eventType: "session.refill_granted",
      correlationId: sessionId,
      payload: {
        latency_ms: Number((process.hrtime.bigint() - refillStartedNs) / 1_000_000n),
        refill_seq: result.refill_seq ?? null,
        funded_value_wei: result.funded_value_wei ?? null,
        cap_status: result.cap_status ?? null,
      },
    });
    return result;
  }

  async closeSession(
    sessionId: string,
    args: { actualUnits: number; outcome?: string; settlement: unknown },
  ): Promise<unknown> {
    const body: Record<string, unknown> = { actual_units: args.actualUnits };
    if (args.outcome !== undefined) body.outcome = args.outcome;
    body.settlement = args.settlement;
    let result: Record<string, unknown>;
    try {
      result = await this.request<Record<string, unknown>>(
        "POST",
        `/v1/sessions/${sessionId}/close`,
        body,
      );
    } catch (exc) {
      this._telemetry.emit({
        eventType: "session.error",
        correlationId: sessionId,
        payload: {
          phase: "close",
          error_class: errorClass(exc),
          error_code: errorProperty(exc, "code") ?? null,
        },
      });
      throw exc;
    }
    this._telemetry.emit({
      eventType: "session.closed",
      correlationId: sessionId,
      payload: {
        actual_units: Number(result.actual_units ?? 0),
        billed_value_wei: result.billed_value_wei ?? null,
        refund_wei: result.refund_wei ?? null,
        outcome: result.outcome ?? null,
        closed_by: "customer",
      },
    });
    return result;
  }

  async getSessionStatus(sessionId: string): Promise<unknown> {
    return this.request("GET", `/v1/sessions/${sessionId}`);
  }

  async getJobStatus(jobId: string): Promise<JobStatus> {
    return this.request<JobStatus>("GET", `/v1/jobs/${jobId}`);
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
        const statusValue = errorProperty(e, "status");
        const status = typeof statusValue === "number" ? statusValue : 0;
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
    extraHeaders: Record<string, string> = {},
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
          ...extraHeaders,
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
