import { fromResponse } from "./errors.js";

export interface Mint {
  payment_id: string;
  work_id: string;
  payment_bytes: string;
  expected_value_wei: string;
  funded_value_wei: string;
  recipient_eth_address: string;
}

export interface Capability {
  name: string;
  work_unit: string | null;
  offerings: {
    id: string;
    price_per_work_unit_wei: string;
    work_unit: string;
  }[];
}

export interface Orchestrator {
  eth_address: string;
  worker_url: string;
  capabilities: Capability[];
  signature_status: string;
  freshness_status: string;
}

export interface UsageReportResult {
  refunded_wei: string;
  payment_status: string;
  new_balance_wei: string;
  usage: { id: string; actual_work_units: number; final_charge_wei: string };
}

export interface RouteView {
  eth_address: string;
  worker_url: string;
  capability: string;
  offering: string;
  price_per_work_unit_wei: string;
}

export interface JobResult {
  body: unknown;
  status: number;
  paymentId: string;
  recipientEthAddress: string;
  requestId: string;
  rawHeaders: Record<string, string>;
}

export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  /** Pass a custom fetch — defaults to global fetch. Useful in tests. */
  fetch?: typeof fetch;
  /** Per-request timeout, ms. Default 15s. */
  timeoutMs?: number;
}

export class OpenClearinghouseClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(opts: ClientOptions) {
    if (!opts.apiKey.startsWith("pymth_")) {
      throw new Error("apiKey looks wrong (expected to start with pymth_)");
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.fetchImpl = opts.fetch ?? globalThis.fetch;
    this.timeoutMs = opts.timeoutMs ?? 15_000;
  }

  // ---- discovery ----

  async listCapabilities(): Promise<Capability[]> {
    const data = await this.request<{ items: Capability[] }>("GET", "/v1/capabilities");
    return data.items;
  }

  async listOrchestrators(opts?: { capability?: string }): Promise<Orchestrator[]> {
    const qs = opts?.capability ? `?capability=${encodeURIComponent(opts.capability)}` : "";
    const data = await this.request<{ items: Orchestrator[] }>("GET", `/v1/orchestrators${qs}`);
    return data.items;
  }

  // ---- payments ----

  async mintPayment(args: {
    capability: string;
    offering: string;
    workUnits: number;
    idempotencyKey?: string;
  }): Promise<Mint> {
    return this.request<Mint>(
      "POST",
      "/v1/payments/mint",
      {
        capability: args.capability,
        offering: args.offering,
        work_units: args.workUnits,
      },
      args.idempotencyKey,
    );
  }

  async reportUsage(args: {
    paymentId: string;
    actualWorkUnits: number;
    idempotencyKey?: string;
  }): Promise<UsageReportResult> {
    return this.request<UsageReportResult>(
      "POST",
      "/v1/usage/report",
      {
        payment_id: args.paymentId,
        actual_work_units: args.actualWorkUnits,
      },
      args.idempotencyKey,
    );
  }

  /**
   * Mint a payment, route to an orchestrator, return its response.
   *
   * The load-bearing convenience method: route selection + payment mint +
   * orch HTTP call with the canonical `POST <broker>/v1/cap` shape and
   * the five Livepeer headers.
   *
   * **Don't put a `model` field in OpenAI-shaped bodies** — the orch
   * routes via `Livepeer-Offering` and most upstreams (vLLM, etc.) will
   * 404 on a mismatched model name. The offering identifies the model.
   */
  async submitJob(args: {
    capability: string;
    offering: string;
    workUnits: number;
    body: unknown;
    idempotencyKey?: string;
    requestId?: string;
    mode?: string;
    specVersion?: string;
    timeoutMs?: number;
  }): Promise<JobResult> {
    // 1. Route — first orch advertising this offering.
    const qs = new URLSearchParams({
      capability: args.capability,
      offering: args.offering,
    });
    const route = await this.request<RouteView>("GET", `/v1/routes?${qs.toString()}`);

    // 2. Mint.
    const mint = await this.mintPayment({
      capability: args.capability,
      offering: args.offering,
      workUnits: args.workUnits,
      ...(args.idempotencyKey === undefined ? {} : { idempotencyKey: args.idempotencyKey }),
    });

    // 3. POST to the orch.
    const requestId = args.requestId ?? crypto.randomUUID();
    const headers: Record<string, string> = {
      "Livepeer-Capability": args.capability,
      "Livepeer-Offering": args.offering,
      "Livepeer-Payment": mint.payment_bytes,
      "Livepeer-Mode": args.mode ?? "http-reqresp@v0",
      "Livepeer-Spec-Version": args.specVersion ?? "0.1",
      "Livepeer-Request-Id": requestId,
    };
    let payload: string | Uint8Array;
    if (args.body instanceof Uint8Array) {
      payload = args.body;
      headers["Content-Type"] ??= "application/octet-stream";
    } else if (typeof args.body === "string") {
      payload = args.body;
      headers["Content-Type"] ??= "application/octet-stream";
    } else {
      payload = JSON.stringify(args.body);
      headers["Content-Type"] = "application/json";
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), args.timeoutMs ?? 60_000);
    let res: Response;
    try {
      res = await this.fetchImpl(`${route.worker_url.replace(/\/+$/, "")}/v1/cap`, {
        method: "POST",
        headers,
        body: payload,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }
    const text = await res.text();
    const ctype = res.headers.get("content-type") ?? "";
    let body: unknown = text;
    if (ctype.includes("json") && text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        // leave as text
      }
    }
    const headerObj: Record<string, string> = {};
    res.headers.forEach((v, k) => {
      headerObj[k] = v;
    });
    return {
      body,
      status: res.status,
      paymentId: mint.payment_id,
      recipientEthAddress: mint.recipient_eth_address,
      requestId,
      rawHeaders: headerObj,
    };
  }

  // ---- internals ----

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
    idempotencyKey?: string,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      "X-API-Key": this.apiKey,
      Accept: "application/json",
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      res = await this.fetchImpl(url, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : null,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }

    let payload: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text };
      }
    }

    if (!res.ok) {
      const retryAfterHeader = res.headers.get("retry-after");
      const retryAfter =
        retryAfterHeader && /^\d+$/.test(retryAfterHeader) ? Number(retryAfterHeader) : null;
      throw fromResponse({ status: res.status, body: payload, retryAfter });
    }
    return payload as T;
  }
}
