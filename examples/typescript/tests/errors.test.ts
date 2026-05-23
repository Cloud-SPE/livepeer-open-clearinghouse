/**
 * Coverage of the code -> constructor mapping in fromResponse(). One
 * `it.each` walks every supported error code so we exercise each
 * subclass constructor (these are otherwise only hit on real errors
 * that may not happen in the lifetime of the test suite).
 */
import { describe, expect, it } from "vitest";

import {
  AccountNotApproved,
  DaemonUnavailable,
  DuplicateRequest,
  EmailNotVerified,
  InsufficientCredit,
  NoRouteAvailable,
  PymtHouseError,
  RateLimited,
  SpendCapExceeded,
} from "../src/index.js";
import { fromResponse } from "../src/errors.js";

describe("fromResponse", () => {
  it.each([
    ["INSUFFICIENT_CREDIT", InsufficientCredit],
    ["SPEND_CAP_EXCEEDED", SpendCapExceeded],
    ["ACCOUNT_NOT_APPROVED", AccountNotApproved],
    ["account_not_approved", AccountNotApproved],
    ["email_not_verified", EmailNotVerified],
    ["NO_ROUTE_AVAILABLE", NoRouteAvailable],
    ["rate_limited", RateLimited],
    ["DUPLICATE_REQUEST", DuplicateRequest],
    ["DAEMON_UNAVAILABLE", DaemonUnavailable],
  ])("maps %s -> %p", (code, Cls) => {
    const err = fromResponse({
      status: 500,
      body: { error: { code, message: "x" } },
      retryAfter: null,
    });
    expect(err).toBeInstanceOf(Cls);
    expect(err.code).toBe(code);
  });

  it("falls back to the base class on unknown codes", () => {
    const err = fromResponse({
      status: 500,
      body: { error: { code: "UNRECOGNIZED", message: "x" } },
      retryAfter: null,
    });
    expect(err).toBeInstanceOf(PymtHouseError);
    expect(err.constructor).toBe(PymtHouseError);
  });

  it("uses `detail` as a fallback for legacy FastAPI errors", () => {
    const err = fromResponse({
      status: 401,
      body: { detail: "invalid api key" },
      retryAfter: null,
    });
    expect(err.code).toBe("invalid api key");
    expect(err.message).toBe("invalid api key");
  });

  it("synthesizes a message when the body has no structured info", () => {
    const err = fromResponse({ status: 599, body: {}, retryAfter: null });
    expect(err.message).toBe("HTTP 599");
  });

  it("stringifies a non-object body into a detail field", () => {
    const err = fromResponse({ status: 500, body: "boom", retryAfter: null });
    expect(err.message).toBe("boom");
  });

  it("preserves retry-after on RateLimited", () => {
    const err = fromResponse({
      status: 429,
      body: { error: { code: "rate_limited", message: "slow down" } },
      retryAfter: 7,
    });
    expect(err).toBeInstanceOf(RateLimited);
    expect((err as RateLimited).retryAfterSeconds).toBe(7);
  });
});
