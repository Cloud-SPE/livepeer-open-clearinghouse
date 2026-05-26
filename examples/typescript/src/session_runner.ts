/**
 * SessionRunner — automatic refill loop for case-(d-extensible) modes.
 *
 * Wraps the broker-side wire (WS or HTTP-control) and the LOC-side
 * refill loop into a single class. Customer code opens a session via
 * the SDK, hands the resulting SessionHandle to SessionRunner, and gets:
 *
 *   - Automatic subscription to Livepeer-Balance-Low from the broker
 *   - Automatic refill: SDK calls LOC's refill endpoint, gets the new
 *     envelope, delivers it to the broker via the mode-specific channel
 *   - Optional callbacks: onRefillSucceeded, onRefillRefused,
 *     onWinddownWarning
 *   - Graceful close on cap-refusal or broker disconnect
 *
 * Mode dispatch mirrors the Python SessionRunner; see
 * `examples/python/src/livepeer_open_clearinghouse_sdk/session_runner.py`
 * for the canonical reference and the docstring covering all four
 * mode classes.
 */

import { WebSocket } from "ws";

import {
  type CapStatus,
  OpenClearinghouseClient,
  type SessionHandle,
} from "./client.js";
import { OpenClearinghouseError } from "./errors.js";

export const BOUNDED_MODES: ReadonlySet<string> = new Set(["ws-realtime@v0"]);

export const WS_TOPUP_MODES: ReadonlySet<string> = new Set([
  "session-control-plus-media@v0",
  "rtmp-ingress-hls-egress@v0",
]);

export const HTTP_TOPUP_MODES: ReadonlySet<string> = new Set([
  "live-session-remote-runner@v0",
  "live-session-gateway-ingest@v0",
]);

export interface RefillEvent {
  refillSeq: number | null;
  expectedValueWei: bigint | null;
  fundedValueWei: bigint | null;
  capStatus: CapStatus | null;
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
  /** Auto-finalize on broker disconnect. Default true. */
  autoCloseOnDisconnect?: boolean;
  /** Inject a custom WebSocket class (for tests / Node 22 native ws). */
  WebSocketCtor?: typeof WebSocket;
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
  private readonly autoCloseOnDisconnect: boolean;
  private readonly WS: typeof WebSocket;

  private ws: WebSocket | null = null;
  private controlTopupUrl: string | null = null;
  private closedResolve: (() => void) | null = null;
  private readonly closedPromise: Promise<void>;
  private finalSettle: FinalSettle | null = null;
  private readonly isBounded: boolean;
  private readonly usesWsTopup: boolean;
  private readonly usesHttpTopup: boolean;

  constructor(opts: SessionRunnerOptions) {
    this.client = opts.client;
    this.handle = opts.handle;
    this.onRefillSucceeded = opts.onRefillSucceeded;
    this.onRefillRefused = opts.onRefillRefused;
    this.onWinddownWarning = opts.onWinddownWarning;
    this.autoCloseOnDisconnect = opts.autoCloseOnDisconnect ?? true;
    this.WS = opts.WebSocketCtor ?? WebSocket;

    this.isBounded = BOUNDED_MODES.has(this.handle.mode);
    this.usesWsTopup = WS_TOPUP_MODES.has(this.handle.mode);
    this.usesHttpTopup = HTTP_TOPUP_MODES.has(this.handle.mode);

    this.closedPromise = new Promise((resolve) => {
      this.closedResolve = resolve;
    });
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

  /** Open the broker-side connection appropriate to the mode. */
  async start(): Promise<void> {
    if (this.isBounded || this.usesWsTopup) {
      await this.openWs();
    } else if (this.usesHttpTopup) {
      await this.openLiveSession();
    } else {
      throw new OpenClearinghouseError({
        code: "unsupported_mode",
        message: `SessionRunner: unsupported mode ${this.handle.mode}`,
        status: 0,
        details: {},
        retryAfterSeconds: null,
      });
    }
  }

  private async openWs(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new this.WS(this.handle.brokerUrl, {
        headers: {
          "Livepeer-Payment": this.handle.paymentEnvelope,
          "Livepeer-Mode": this.handle.mode,
        },
      });
      this.ws = ws;
      ws.on("open", () => resolve());
      ws.on("error", reject);
      ws.on("message", (data: Buffer | ArrayBuffer | Buffer[]) => {
        void this.handleWsMessage(data);
      });
      ws.on("close", () => {
        if (this.autoCloseOnDisconnect && this.finalSettle === null) {
          void this.close({ actualUnits: 0 });
        }
      });
    });
  }

  private async openLiveSession(): Promise<void> {
    const resp = await fetch(
      `${this.handle.brokerUrl.replace(/\/+$/, "")}/v1/cap`,
      {
        method: "POST",
        headers: {
          "Livepeer-Payment": this.handle.paymentEnvelope,
          "Livepeer-Mode": this.handle.mode,
          "Content-Type": "application/json",
        },
        body: "{}",
      },
    );
    if (!resp.ok) {
      throw new OpenClearinghouseError({
        code: "protocol_error",
        message: `broker session-open failed: ${resp.status}`,
        status: resp.status,
        details: {},
        retryAfterSeconds: null,
      });
    }
    const data = (await resp.json()) as { control?: { topup_url?: string } };
    this.controlTopupUrl = data.control?.topup_url ?? null;
    if (!this.controlTopupUrl) {
      throw new OpenClearinghouseError({
        code: "protocol_error",
        message: "broker session-open response missing control.topup_url",
        status: 0,
        details: {},
        retryAfterSeconds: null,
      });
    }
  }

  private async handleWsMessage(
    data: Buffer | ArrayBuffer | Buffer[],
  ): Promise<void> {
    let text: string;
    if (typeof data === "string") {
      text = data;
    } else if (Buffer.isBuffer(data)) {
      text = data.toString("utf8");
    } else if (Array.isArray(data)) {
      text = Buffer.concat(data).toString("utf8");
    } else {
      // Binary capability payload — not our concern
      return;
    }
    let payload: { type?: string; observed_consumed_units?: number; projected_end_at?: string };
    try {
      payload = JSON.parse(text);
    } catch {
      return;
    }
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.type === "session.balance.low" || payload.type === "Livepeer-Balance-Low") {
      await this.onBalanceLow(payload);
    }
  }

  /**
   * Trigger the balance-low handling explicitly. Useful for HTTP-topup
   * modes where the customer's media-plane code observes balance-low
   * out-of-band and routes it to the runner.
   */
  async onBalanceLow(payload: {
    observed_consumed_units?: number;
    projected_end_at?: string;
  }): Promise<void> {
    if (this.isBounded) {
      await this.fireWinddown({
        reason: "ws_session_exhausting",
        projectedEndAt: payload.projected_end_at ?? null,
      });
      return;
    }

    let refill: {
      payment_envelope: string;
      refill_seq?: number;
      expected_value_wei?: number;
      funded_value_wei?: number;
      cap_status?: CapStatus;
    };
    try {
      const refillOpts: { observedConsumedUnits?: number } = {};
      if (payload.observed_consumed_units !== undefined) {
        refillOpts.observedConsumedUnits = payload.observed_consumed_units;
      }
      refill = (await this.client.refillSession(
        this.handle.sessionId,
        refillOpts,
      )) as typeof refill;
    } catch (err) {
      if (err instanceof OpenClearinghouseError) {
        await this.fireRefillRefused({
          refillSeq: null,
          expectedValueWei: null,
          fundedValueWei: null,
          capStatus: null,
          error: err,
        });
        return;
      }
      throw err;
    }

    if (this.usesWsTopup) {
      await this.deliverTopupWs(refill.payment_envelope);
    } else if (this.usesHttpTopup) {
      await this.deliverTopupHttp(refill.payment_envelope);
    }

    await this.fireRefillSucceeded({
      refillSeq: refill.refill_seq ?? null,
      expectedValueWei:
        refill.expected_value_wei != null ? BigInt(refill.expected_value_wei) : null,
      fundedValueWei:
        refill.funded_value_wei != null ? BigInt(refill.funded_value_wei) : null,
      capStatus: refill.cap_status ?? null,
      error: null,
    });

    if (refill.cap_status?.will_refuse_next_refill) {
      await this.fireWinddown({
        reason: refill.cap_status.winddown_reason ?? "cap_imminent",
        projectedEndAt: null,
      });
    }
  }

  private async deliverTopupWs(envelope: string): Promise<void> {
    if (this.ws === null) {
      throw new OpenClearinghouseError({
        code: "internal",
        message: "ws not open",
        status: 0,
        details: {},
        retryAfterSeconds: null,
      });
    }
    const frame = JSON.stringify({
      type: "session.topup",
      body: { payment_header: envelope },
    });
    await new Promise<void>((resolve, reject) => {
      this.ws!.send(frame, (err) => (err ? reject(err) : resolve()));
    });
  }

  private async deliverTopupHttp(envelope: string): Promise<void> {
    if (this.controlTopupUrl === null) {
      throw new OpenClearinghouseError({
        code: "internal",
        message: "topup_url not captured",
        status: 0,
        details: {},
        retryAfterSeconds: null,
      });
    }
    const resp = await fetch(this.controlTopupUrl, {
      method: "POST",
      headers: {
        "Livepeer-Payment": envelope,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ gateway_session_id: this.handle.sessionId }),
    });
    if (!resp.ok) {
      throw new OpenClearinghouseError({
        code: "protocol_error",
        message: `broker topup failed: ${resp.status}`,
        status: resp.status,
        details: {},
        retryAfterSeconds: null,
      });
    }
  }

  private async fireRefillSucceeded(event: RefillEvent): Promise<void> {
    if (this.onRefillSucceeded) {
      await this.onRefillSucceeded(event);
    }
  }

  private async fireRefillRefused(event: RefillEvent): Promise<void> {
    if (this.onRefillRefused) {
      await this.onRefillRefused(event);
    }
  }

  private async fireWinddown(event: WinddownEvent): Promise<void> {
    if (this.onWinddownWarning) {
      await this.onWinddownWarning(event);
    }
  }

  /** Close the session and finalize accounting on LOC. Idempotent. */
  async close(args: {
    actualUnits: number;
    outcome?: string;
    settlement?: unknown;
  }): Promise<FinalSettle> {
    if (this.finalSettle !== null) {
      return this.finalSettle;
    }
    if (this.ws !== null) {
      try {
        this.ws.close();
      } catch {
        // already closed
      }
    }
    const result = (await this.client.closeSession(this.handle.sessionId, args)) as FinalSettle;
    this.finalSettle = result;
    this.closedResolve?.();
    return result;
  }

  /** Resolves when the session is closed (by any path). */
  waitClosed(): Promise<void> {
    return this.closedPromise;
  }
}
