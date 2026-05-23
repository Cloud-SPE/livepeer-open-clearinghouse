/**
 * Typed errors mapped from PymtHouse's response envelope:
 *
 *     { "error": { "code": "...", "message": "...", "details": {...} } }
 *
 * Anything we don't recognize falls through to the base PymtHouseError
 * so callers can still log + retry sensibly.
 */

export interface ErrorBody {
  status: number;
  code: string | null;
  message: string;
  details: Record<string, unknown>;
  retryAfterSeconds: number | null;
}

export class PymtHouseError extends Error {
  readonly code: string | null;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(body: ErrorBody) {
    super(body.message);
    this.name = "PymtHouseError";
    this.code = body.code;
    this.status = body.status;
    this.details = body.details;
  }
}

export class InsufficientCredit extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "InsufficientCredit";
  }
}

export class SpendCapExceeded extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "SpendCapExceeded";
  }
}

export class AccountNotApproved extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "AccountNotApproved";
  }
}

export class EmailNotVerified extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "EmailNotVerified";
  }
}

export class NoRouteAvailable extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "NoRouteAvailable";
  }
}

export class RateLimited extends PymtHouseError {
  readonly retryAfterSeconds: number | null;
  constructor(body: ErrorBody) {
    super(body);
    this.name = "RateLimited";
    this.retryAfterSeconds = body.retryAfterSeconds;
  }
}

export class DuplicateRequest extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "DuplicateRequest";
  }
}

export class DaemonUnavailable extends PymtHouseError {
  constructor(body: ErrorBody) {
    super(body);
    this.name = "DaemonUnavailable";
  }
}

const CODE_MAP: Record<string, new (body: ErrorBody) => PymtHouseError> = {
  INSUFFICIENT_CREDIT: InsufficientCredit,
  SPEND_CAP_EXCEEDED: SpendCapExceeded,
  ACCOUNT_NOT_APPROVED: AccountNotApproved,
  account_not_approved: AccountNotApproved,
  email_not_verified: EmailNotVerified,
  NO_ROUTE_AVAILABLE: NoRouteAvailable,
  rate_limited: RateLimited,
  DUPLICATE_REQUEST: DuplicateRequest,
  DAEMON_UNAVAILABLE: DaemonUnavailable,
};

export function fromResponse(args: {
  status: number;
  body: unknown;
  retryAfter: number | null;
}): PymtHouseError {
  const dict = isRecord(args.body) ? args.body : { detail: String(args.body) };
  const envelope = isRecord(dict.error) ? dict.error : {};
  const envCode = envelope.code;
  const envMessage = envelope.message;
  const dictDetail = dict.detail;
  const code =
    (typeof envCode === "string" ? envCode : null) ??
    (typeof dictDetail === "string" ? dictDetail : null);
  const message =
    (typeof envMessage === "string" ? envMessage : null) ??
    (typeof dictDetail === "string" ? dictDetail : null) ??
    `HTTP ${String(args.status)}`;
  const details = isRecord(envelope.details) ? envelope.details : {};

  const body: ErrorBody = {
    status: args.status,
    code,
    message,
    details,
    retryAfterSeconds: args.retryAfter,
  };
  const Cls = code ? (CODE_MAP[code] ?? PymtHouseError) : PymtHouseError;
  return new Cls(body);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
