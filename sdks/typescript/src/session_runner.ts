/** paid-session/v1 broker control driver with idempotent HTTP refills. */

import { WebSocket } from "ws";

import { OpenClearinghouseClient, type SessionHandle } from "./client.js";
import { BrokerProtocolError, OpenClearinghouseError } from "./errors.js";

export interface SessionBalance {
  status: "ok" | "low" | "exhausted";
  claimed_units: number;
  debited_units: number;
  unit: string;
  runway_units: number;
  runway_seconds_estimate: number | null;
  will_refuse_next_refill: boolean;
}

export interface BrokerSession {
  sessionId: string;
  workId: string;
  state: string;
  runtime: {
    schema: string;
    public: Record<string, unknown>;
    grants: Record<string, unknown>[];
  };
  credential: string;
  leaseExpiresAt: string;
  balance: SessionBalance;
  control: {
    statusUrl: string;
    topupUrl: string;
    endUrl: string;
    eventsWs: string | null;
  };
}

export interface RefillEvent {
  refillSeq: number | null;
  expectedValueWei: bigint | null;
  fundedValueWei: bigint | null;
  capStatus: Record<string, unknown> | null;
  error: OpenClearinghouseError | null;
}

export interface WinddownEvent {
  reason: string;
  projectedEndAt: string | null;
}

export type RefillCallback = (event: RefillEvent) => void | Promise<void>;
export type WinddownCallback = (event: WinddownEvent) => void | Promise<void>;

export interface SessionRunnerOptions {
  client: OpenClearinghouseClient;
  handle: SessionHandle;
  onRefillSucceeded?: RefillCallback;
  onRefillRefused?: RefillCallback;
  onWinddownWarning?: WinddownCallback;
  WebSocketCtor?: typeof WebSocket;
  fetch?: typeof fetch;
}

interface FinalSettle {
  outcome: string;
  billed_value_wei: number;
  refund_wei: number;
}

export class SessionRunner {
  private readonly client: OpenClearinghouseClient;
  private readonly handle: SessionHandle;
  private readonly onRefillSucceeded: RefillCallback | undefined;
  private readonly onRefillRefused: RefillCallback | undefined;
  private readonly onWinddownWarning: WinddownCallback | undefined;
  private readonly WS: typeof WebSocket;
  private readonly fetchImpl: typeof fetch;
  private ws: WebSocket | null = null;
  private broker: BrokerSession | null = null;
  private pendingRefillKey: string | null = null;
  private pendingRefill: Record<string, unknown> | null = null;
  private finalSettle: FinalSettle | null = null;
  private closedResolve: (() => void) | null = null;
  private readonly closedPromise: Promise<void>;

  constructor(opts: SessionRunnerOptions) {
    this.client = opts.client;
    this.handle = opts.handle;
    this.onRefillSucceeded = opts.onRefillSucceeded;
    this.onRefillRefused = opts.onRefillRefused;
    this.onWinddownWarning = opts.onWinddownWarning;
    this.WS = opts.WebSocketCtor ?? WebSocket;
    this.fetchImpl = opts.fetch ?? fetch;
    this.closedPromise = new Promise((resolve) => {
      this.closedResolve = resolve;
    });
  }

  get brokerSession(): BrokerSession | null {
    return this.broker;
  }

  get outcome(): string | null {
    return this.finalSettle?.outcome ?? null;
  }

  get billedValueWei(): bigint | null {
    return this.finalSettle ? BigInt(this.finalSettle.billed_value_wei) : null;
  }

  get refundWei(): bigint | null {
    return this.finalSettle ? BigInt(this.finalSettle.refund_wei) : null;
  }

  async start(): Promise<BrokerSession> {
    if (this.broker !== null) return this.broker;
    const response = await this.fetchImpl(
      `${this.handle.brokerUrl.replace(/\/+$/, "")}/v1/session`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Livepeer-Protocol": this.handle.protocol,
          "Livepeer-Capability": this.handle.capability,
          "Livepeer-Offering": this.handle.offering,
          "Livepeer-Request-Id": this.handle.requestId,
          "Livepeer-Payment": this.handle.paymentEnvelope,
        },
        body: JSON.stringify({
          gateway_session_id: this.handle.sessionId,
          session_params: this.handle.sessionParams,
        }),
      },
    );
    if (!response.ok) throw protocolError(`broker session-open failed: ${String(response.status)}`);
    this.broker = parseOpen(await response.json(), this.handle);
    if (this.broker.control.eventsWs !== null) {
      this.ws = new this.WS(this.broker.control.eventsWs, {
        headers: { Authorization: `Bearer ${this.broker.credential}` },
      });
      this.ws.on("message", (data: Buffer | ArrayBuffer | Buffer[]) => {
        void this.handleWsMessage(data);
      });
    }
    return this.broker;
  }

  async status(): Promise<Record<string, unknown>> {
    const session = await this.start();
    const response = await this.fetchImpl(session.control.statusUrl, {
      headers: { Authorization: `Bearer ${session.credential}` },
    });
    if (!response.ok) throw protocolError(`broker status failed: ${String(response.status)}`);
    return (await response.json()) as Record<string, unknown>;
  }

  async onBalance(value: SessionBalance): Promise<void> {
    const balance = parseBalance(value);
    if (balance.will_refuse_next_refill) {
      await this.onWinddownWarning?.({
        reason: "broker_will_refuse_next_refill",
        projectedEndAt: null,
      });
      return;
    }
    if (balance.status !== "low") return;
    if (this.handle.session.refill === "bounded") {
      await this.onWinddownWarning?.({
        reason: "bounded_runway_exhausting",
        projectedEndAt: null,
      });
      return;
    }
    await this.refill(balance.claimed_units);
  }

  private async refill(observedUnits: number): Promise<void> {
    const session = await this.start();
    this.pendingRefillKey ??= crypto.randomUUID();
    if (this.pendingRefill === null) {
      try {
        this.pendingRefill = (await this.client.refillSession(this.handle.sessionId, {
          observedConsumedUnits: observedUnits,
          requestId: this.pendingRefillKey,
        })) as Record<string, unknown>;
      } catch (error) {
        if (error instanceof OpenClearinghouseError) {
          await this.onRefillRefused?.({
            refillSeq: null,
            expectedValueWei: null,
            fundedValueWei: null,
            capStatus: null,
            error,
          });
          return;
        }
        throw error;
      }
    }
    const refill = this.pendingRefill;
    let response = await this.postTopup(session, refill);
    if (brokerError(response) === "recipient_rotated") {
      if (refill.rebind_from !== null && refill.rebind_from !== undefined) {
        await this.endUnrecoverableRotation();
        return;
      }
      const predecessor = String(refill.work_id);
      const replacementKey = crypto.randomUUID();
      this.pendingRefillKey = replacementKey;
      try {
        this.pendingRefill = (await this.client.refillSession(this.handle.sessionId, {
          observedConsumedUnits: observedUnits,
          requestId: replacementKey,
          rebindFrom: predecessor,
          replacesRequestId: String(refill.request_id),
        })) as Record<string, unknown>;
      } catch (error) {
        if (error instanceof OpenClearinghouseError) {
          await this.onRefillRefused?.({
            refillSeq: null,
            expectedValueWei: null,
            fundedValueWei: null,
            capStatus: null,
            error,
          });
          return;
        }
        throw error;
      }
      response = await this.postTopup(session, this.pendingRefill);
    }
    if (brokerError(response) === "recipient_rotated") {
      await this.endUnrecoverableRotation();
      return;
    }
    if (brokerError(response) === "rebind_refused") {
      await this.endUnrecoverableRotation();
      return;
    }
    if (!response.ok) throw protocolError(`broker topup failed: ${String(response.status)}`);
    const acceptedRefill = this.pendingRefill;
    if (acceptedRefill.rebind_from !== null && acceptedRefill.rebind_from !== undefined) {
      session.workId = String(acceptedRefill.work_id);
    }
    const brokerResult = (await response.json()) as { balance?: SessionBalance };
    if (brokerResult.balance?.will_refuse_next_refill) {
      await this.onWinddownWarning?.({
        reason: "broker_will_refuse_next_refill",
        projectedEndAt: null,
      });
    }
    await this.onRefillSucceeded?.({
      refillSeq: numberOrNull(acceptedRefill.refill_seq),
      expectedValueWei: bigintOrNull(acceptedRefill.expected_value_wei),
      fundedValueWei: bigintOrNull(acceptedRefill.funded_value_wei),
      capStatus: (acceptedRefill.cap_status as Record<string, unknown> | undefined) ?? null,
      error: null,
    });
    this.pendingRefill = null;
    this.pendingRefillKey = null;
  }

  private async postTopup(
    session: BrokerSession,
    refill: Record<string, unknown>,
  ): Promise<Response> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${session.credential}`,
      "Content-Type": "application/json",
      "Livepeer-Payment": String(refill.payment_envelope),
      "Livepeer-Request-Id": String(refill.request_id),
    };
    const rebindFrom = refill.rebind_from;
    if (rebindFrom !== null && rebindFrom !== undefined) {
      if (typeof rebindFrom !== "string" || rebindFrom.length === 0) {
        throw protocolError("LOC returned an invalid rotation predecessor");
      }
      headers["Livepeer-Rebind-From"] = rebindFrom;
    }
    return this.fetchImpl(session.control.topupUrl, {
      method: "POST",
      headers,
      body: "{}",
    });
  }

  private async endUnrecoverableRotation(): Promise<void> {
    this.pendingRefill = null;
    this.pendingRefillKey = null;
    await this.onWinddownWarning?.({
      reason: "payment_unrecoverable",
      projectedEndAt: null,
    });
  }

  private async handleWsMessage(data: Buffer | ArrayBuffer | Buffer[]): Promise<void> {
    const text = Buffer.isBuffer(data)
      ? data.toString("utf8")
      : Array.isArray(data)
        ? Buffer.concat(data).toString("utf8")
        : null;
    if (text === null) return;
    try {
      const payload = JSON.parse(text) as { type?: string; balance?: SessionBalance };
      if (payload.type === "session.balance" && payload.balance) {
        await this.onBalance(payload.balance);
      }
    } catch {
      // Ignore non-control frames.
    }
  }

  async close(args: { actualUnits: number; outcome?: string }): Promise<FinalSettle> {
    if (this.finalSettle !== null) return this.finalSettle;
    const session = await this.start();
    const response = await this.fetchImpl(session.control.endUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.credential}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ reason: "gateway_close" }),
    });
    if (!response.ok) throw protocolError(`broker end failed: ${String(response.status)}`);
    const encodedSettlement = response.headers.get("livepeer-settlement");
    if (!encodedSettlement) {
      throw protocolError("broker end response missing Livepeer-Settlement");
    }
    let settlement: unknown;
    try {
      const bytes = Uint8Array.from(atob(encodedSettlement), (char) => char.charCodeAt(0));
      settlement = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
    } catch {
      throw protocolError("broker end response has malformed Livepeer-Settlement");
    }
    this.ws?.close();
    this.finalSettle = (await this.client.closeSession(this.handle.sessionId, {
      ...args,
      settlement,
    })) as FinalSettle;
    this.closedResolve?.();
    return this.finalSettle;
  }

  waitClosed(): Promise<void> {
    return this.closedPromise;
  }
}

function parseOpen(value: unknown, handle: SessionHandle): BrokerSession {
  const data = asRecord(value, "malformed broker open");
  const runtime = asRecord(data.runtime, "malformed broker runtime");
  const lease = asRecord(data.lease, "malformed broker lease");
  const control = asRecord(data.control, "malformed broker control");
  if (data.work_id !== handle.workId) throw protocolError("broker work_id mismatch");
  if (runtime.schema !== handle.session.descriptor_schema) {
    throw protocolError("broker descriptor schema mismatch");
  }
  const runtimePublic = asRecord(runtime.public, "malformed broker runtime.public");
  if (!Array.isArray(runtime.grants)) throw protocolError("malformed broker runtime.grants");
  const grants = runtime.grants.map((grant) => asRecord(grant, "malformed broker grant"));
  return {
    sessionId: requiredString(data, "session_id"),
    workId: requiredString(data, "work_id"),
    state: requiredString(data, "state"),
    runtime: {
      schema: requiredString(runtime, "schema"),
      public: runtimePublic,
      grants,
    },
    credential: requiredString(data, "credential"),
    leaseExpiresAt: requiredString(lease, "expires_at"),
    balance: parseBalance(data.balance),
    control: {
      statusUrl: requiredString(control, "status_url"),
      topupUrl: requiredString(control, "topup_url"),
      endUrl: requiredString(control, "end_url"),
      eventsWs: optionalString(control, "events_ws"),
    },
  };
}

function parseBalance(value: unknown): SessionBalance {
  const balance = asRecord(value, "malformed broker balance");
  if (!["ok", "low", "exhausted"].includes(String(balance.status))) {
    throw protocolError("malformed broker balance");
  }
  return balance as unknown as SessionBalance;
}

function brokerError(response: Response): string | null {
  return response.status === 409 ? response.headers.get("Livepeer-Error") : null;
}

function requiredString(value: Record<string, unknown>, key: string): string {
  const result = value[key];
  if (typeof result !== "string" || result.length === 0) throw protocolError(`missing ${key}`);
  return result;
}

function optionalString(value: Record<string, unknown>, key: string): string | null {
  const result = value[key];
  if (result == null) return null;
  if (typeof result !== "string" || result.length === 0) throw protocolError(`invalid ${key}`);
  return result;
}

function asRecord(value: unknown, message: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw protocolError(message);
  }
  return value as Record<string, unknown>;
}

function protocolError(message: string): BrokerProtocolError {
  return new BrokerProtocolError(message);
}

function numberOrNull(value: unknown): number | null {
  return value == null ? null : Number(value);
}

function bigintOrNull(value: unknown): bigint | null {
  return value == null ? null : BigInt(value as string | number);
}
