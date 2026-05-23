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
  offerings: Array<{
    id: string;
    price_per_work_unit_wei: string;
    work_unit: string;
  }>;
}

export interface Orchestrator {
  eth_address: string;
  service_url: string;
  capabilities: string[];
  freshness_status: string;
}

export interface UsageReportResult {
  refunded_wei: string;
  payment_status: string;
  new_balance_wei: string;
  usage: { id: string; actual_work_units: number; final_charge_wei: string };
}

export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  /** Pass a custom fetch — defaults to global fetch. Useful in tests. */
  fetch?: typeof fetch;
  /** Per-request timeout, ms. Default 15s. */
  timeoutMs?: number;
}

export class PymtHouseClient {
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
    const qs = opts?.capability
      ? `?capability=${encodeURIComponent(opts.capability)}`
      : "";
    const data = await this.request<{ items: Orchestrator[] }>(
      "GET",
      `/v1/orchestrators${qs}`,
    );
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
        retryAfterHeader && /^\d+$/.test(retryAfterHeader)
          ? Number(retryAfterHeader)
          : null;
      throw fromResponse({ status: res.status, body: payload, retryAfter });
    }
    return payload as T;
  }
}
