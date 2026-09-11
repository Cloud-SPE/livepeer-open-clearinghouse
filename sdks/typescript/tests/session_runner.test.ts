import { describe, expect, it } from "vitest";

import {
  OpenClearinghouseClient,
  type SessionBalance,
  type SessionHandle,
  SessionRunner,
  type WinddownEvent,
} from "../src/index.js";

const LOC = "http://loc.test";
const BROKER = "http://broker.test";
const SID = "11111111-1111-1111-1111-111111111111";

function handle(refill: "bounded" | "extensible" = "extensible"): SessionHandle {
  return {
    sessionId: SID,
    requestId: "open-request",
    workId: "wid",
    brokerUrl: BROKER,
    protocol: "paid-session/v1",
    capability: "livepeer:test",
    offering: "default",
    session: {
      descriptor_schema: "livepeer-session-test/v1",
      attachment: "external",
      metering: "runner-reported",
      refill,
    },
    sessionParams: { room: "alpha" },
    spendAuthorization: Buffer.from("initial").toString("base64"),
    accountingMode: "wholesale_account",
    callerProof: "INITIAL-PROOF",
    sessionOpenBody: JSON.stringify({ gateway_session_id: SID, session_params: { room: "alpha" } }),
    maxTotalUnits: 100,
    signCallerProof: () => "PROOF",
    expectedValueWei: 100_000n,
    fundedValueWei: 100_000n,
    refillEndpoint: `/v1/sessions/${SID}/refill`,
    closeEndpoint: `/v1/sessions/${SID}/close`,
  };
}

function balance(overrides: Partial<SessionBalance> = {}): SessionBalance {
  return {
    status: "ok",
    claimed_units: 10,
    debited_units: 10,
    unit: "participant_minutes",
    runway_units: 90,
    runway_seconds_estimate: 5400,
    will_refuse_next_refill: false,
    ...overrides,
  };
}

function openResponse(schema = "livepeer-session-test/v1"): unknown {
  return {
    session_id: "broker-session",
    work_id: "wid",
    state: "active",
    runtime: { schema, public: { url: "https://runtime.test" }, grants: [] },
    credential: "credential",
    lease: { expires_at: "2026-08-21T00:00:00Z" },
    balance: balance(),
    control: {
      status_url: `${BROKER}/status`,
      topup_url: `${BROKER}/topup`,
      end_url: `${BROKER}/end`,
    },
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("SessionRunner paid-session/v1", () => {
  it("opens, refills, and ends over the authoritative HTTP contract", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    let topupAttempts = 0;
    const fetchImpl: typeof fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      calls.push({ url, ...(init === undefined ? {} : { init }) });
      if (url === `${BROKER}/v1/session`) return Promise.resolve(json(openResponse()));
      if (url === `${LOC}/v1/sessions/${SID}/refill`) {
        return Promise.resolve(
          json({
            request_id: "refill-request",
            refill_seq: 1,
            work_id: "loc-auth:revision",
            spend_authorization: btoa("revision"),
            accounting_mode: "wholesale_account",
            expected_value_wei: "50000",
            funded_value_wei: "50000",
            cap_status: null,
          }),
        );
      }
      if (url === `${BROKER}/status`)
        return Promise.resolve(json({ state: "active", balance: balance() }));
      if (url === `${BROKER}/topup`) {
        topupAttempts += 1;
        return Promise.resolve(
          topupAttempts === 1 ? json({ error: "retry" }, 503) : json({ balance: balance() }),
        );
      }
      if (url === `${BROKER}/end`) {
        return Promise.resolve(
          new Response(null, {
            status: 204,
            headers: {
              "Livepeer-Settlement": "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0=",
            },
          }),
        );
      }
      if (url === `${LOC}/v1/sessions/${SID}/close`) {
        return Promise.resolve(
          json({ outcome: "EXACT", billed_value_wei: "150000", refund_wei: "0" }),
        );
      }
      return Promise.resolve(json({ detail: "unmatched" }, 404));
    };
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    await new SessionRunner({ client, handle: handle(), fetch: fetchImpl }).start();
    const runner = new SessionRunner({
      client,
      handle: handle(),
      fetch: fetchImpl,
      approveCapExtension: () => 200,
    });
    await runner.start();
    expect((await runner.status()).state).toBe("active");
    const low = balance({ status: "low", claimed_units: 80 });
    await expect(runner.onBalance(low)).rejects.toThrow(/topup/);
    await runner.onBalance(low);
    const result = await runner.close({ actualUnits: 150 });

    expect(result.outcome).toBe("EXACT");
    expect(runner.billedValueWei).toBe(150_000n);
    expect(runner.refundWei).toBe(0n);
    const closeCall = calls.find((call) => call.url.endsWith(`/sessions/${SID}/close`));
    const closeBody = closeCall?.init?.body;
    if (typeof closeBody !== "string") throw new Error("missing close body");
    expect(JSON.parse(closeBody).settlement).toEqual({
      payload: {},
      signature: {},
    });
    const opened = calls.find((call) => call.url.endsWith("/v1/session"));
    expect(opened).toBeDefined();
    if (opened?.init === undefined) throw new Error("missing broker open call");
    expect((opened.init.headers as Record<string, string>)["Livepeer-Protocol"]).toBe(
      "paid-session/v1",
    );
    const topup = calls.find((call) => call.url.endsWith("/topup"));
    expect(topup).toBeDefined();
    if (topup?.init === undefined) throw new Error("missing broker top-up call");
    expect((topup.init.headers as Record<string, string>)["Livepeer-Request-Id"]).toBe(
      "refill-request",
    );
    expect(calls.filter((call) => call.url.endsWith("/v1/session"))).toHaveLength(2);
    expect(calls.filter((call) => call.url.includes("/refill"))).toHaveLength(1);
    expect(calls.filter((call) => call.url.endsWith("/topup"))).toHaveLength(2);
  });

  it("drains bounded and warned sessions without refilling", async () => {
    const warnings: WinddownEvent[] = [];
    const fetchImpl: typeof fetch = () => Promise.resolve(json(openResponse()));
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    const runner = new SessionRunner({
      client,
      handle: handle("bounded"),
      fetch: fetchImpl,
      onWinddownWarning: (event) => {
        warnings.push(event);
      },
    });
    await runner.start();
    await runner.onBalance(balance({ status: "low" }));
    await runner.onBalance(balance({ will_refuse_next_refill: true }));
    expect(warnings.map((event) => event.reason)).toEqual([
      "bounded_runway_exhausting",
      "broker_will_refuse_next_refill",
    ]);
  });

  it("winds down wholesale low balance without explicit cap approval", async () => {
    const warnings: WinddownEvent[] = [];
    const wholesale = {
      ...handle(),
      spendAuthorization: Buffer.from("initial").toString("base64"),
      accountingMode: "wholesale_account" as const,
      callerProof: "INITIAL-PROOF",
      maxTotalUnits: 100,
    };
    const client = new OpenClearinghouseClient({ baseUrl: LOC, apiKey: "pymth_test" });
    const runner = new SessionRunner({
      client,
      handle: wholesale,
      onWinddownWarning: (event) => {
        warnings.push(event);
      },
    });
    await runner.onBalance(balance({ status: "low" }));
    expect(warnings.map((event) => event.reason)).toEqual(["wholesale_cap_extension_required"]);
  });

  it("commits and signs the exact wholesale cap-revision body", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const successor = Buffer.from("successor").toString("base64");
    const fetchImpl: typeof fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      calls.push({ url, ...(init === undefined ? {} : { init }) });
      if (url === `${BROKER}/v1/session`) return Promise.resolve(json(openResponse()));
      if (url === `${LOC}/v1/sessions/${SID}/refill`) {
        return Promise.resolve(
          json({
            work_id: "loc-auth:revision",
            request_id: "revision-request",
            refill_seq: 1,
            payment_envelope: null,
            spend_authorization: successor,
            accounting_mode: "wholesale_account",
            expected_value_wei: "0",
            funded_value_wei: "0",
            cap_status: null,
          }),
        );
      }
      if (url === `${BROKER}/topup`) return Promise.resolve(json({ balance: balance() }));
      return Promise.resolve(json({}, 404));
    };
    const wholesale = {
      ...handle(),
      spendAuthorization: Buffer.from("initial").toString("base64"),
      accountingMode: "wholesale_account" as const,
      callerProof: "INITIAL-PROOF",
      maxTotalUnits: 100,
      signCallerProof: (bytes: Uint8Array) => {
        expect(Buffer.from(bytes).toString()).toBe("successor");
        return "SUCCESSOR-PROOF";
      },
    };
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    const runner = new SessionRunner({
      client,
      handle: wholesale,
      fetch: fetchImpl,
      approveCapExtension: () => 200,
    });
    await runner.start();
    await runner.onBalance(balance({ status: "low", claimed_units: 80 }));

    const revision = calls.find((call) => call.url.includes("/refill"));
    const topup = calls.find((call) => call.url.endsWith("/topup"));
    if (typeof revision?.init?.body !== "string" || typeof topup?.init?.body !== "string") {
      throw new Error("missing wholesale revision bodies");
    }
    expect(JSON.parse(revision.init.body)).toMatchObject({
      observed_consumed_units: 80,
      max_total_units: 200,
      workload_request_digest: "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    });
    expect(topup.init.body).toBe("{}");
    const headers = topup.init.headers as Record<string, string>;
    expect(headers["Livepeer-Authorization"]).toBe(successor);
    expect(headers["Livepeer-Caller-Proof"]).toBe("SUCCESSOR-PROOF");
    expect(headers["Livepeer-Payment"]).toBeUndefined();
  });

  it.skip("remints recipient rotation with a fresh id and declared predecessor", async () => {
    const warnings: WinddownEvent[] = [];
    const calls: { url: string; init?: RequestInit }[] = [];
    let refillCount = 0;
    let topupCount = 0;
    const fetchImpl: typeof fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      calls.push({ url, ...(init === undefined ? {} : { init }) });
      if (url === `${BROKER}/v1/session`) return Promise.resolve(json(openResponse()));
      if (url === `${LOC}/v1/sessions/${SID}/refill`) {
        refillCount += 1;
        return Promise.resolve(
          json(
            refillCount === 1
              ? {
                  work_id: "wid",
                  request_id: "rejected-request",
                  payment_envelope: "REJECTED-ENV",
                }
              : {
                  work_id: "wid-successor",
                  request_id: "successor-request",
                  payment_envelope: "SUCCESSOR-ENV",
                  rebind_from: "wid",
                },
          ),
        );
      }
      if (url === `${BROKER}/topup`) {
        topupCount += 1;
        return Promise.resolve(
          topupCount === 1
            ? new Response(null, {
                status: 409,
                headers: { "Livepeer-Error": "recipient_rotated" },
              })
            : json({ balance: balance() }),
        );
      }
      return Promise.resolve(json({ detail: "unmatched" }, 404));
    };
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    const runner = new SessionRunner({
      client,
      handle: handle(),
      fetch: fetchImpl,
      onWinddownWarning: (event) => {
        warnings.push(event);
      },
    });
    await runner.start();
    await runner.onBalance(balance({ status: "low" }));

    const refills = calls.filter((call) => call.url.includes("/refill"));
    const topups = calls.filter((call) => call.url.endsWith("/topup"));
    expect(refills).toHaveLength(2);
    const replacementBody = refills[1]?.init?.body;
    if (typeof replacementBody !== "string") throw new Error("missing replacement body");
    expect(JSON.parse(replacementBody)).toMatchObject({
      rebind_from: "wid",
      replaces_request_id: "rejected-request",
    });
    expect((refills[0]?.init?.headers as Record<string, string>)["Idempotency-Key"]).not.toBe(
      (refills[1]?.init?.headers as Record<string, string>)["Idempotency-Key"],
    );
    expect((topups[1]?.init?.headers as Record<string, string>)["Livepeer-Rebind-From"]).toBe(
      "wid",
    );
    expect((topups[1]?.init?.headers as Record<string, string>)["Livepeer-Request-Id"]).toBe(
      "successor-request",
    );
    expect(runner.brokerSession?.workId).toBe("wid-successor");
    expect(warnings).toEqual([]);
  });

  it.skip("drains when the broker refuses a declared rebind", async () => {
    const warnings: WinddownEvent[] = [];
    let refillCount = 0;
    let topupCount = 0;
    const fetchImpl: typeof fetch = (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url === `${BROKER}/v1/session`) return Promise.resolve(json(openResponse()));
      if (url === `${LOC}/v1/sessions/${SID}/refill`) {
        refillCount += 1;
        return Promise.resolve(
          json(
            refillCount === 1
              ? { work_id: "wid", request_id: "rejected", payment_envelope: "OLD" }
              : {
                  work_id: "new",
                  request_id: "successor",
                  payment_envelope: "NEW",
                  rebind_from: "wid",
                },
          ),
        );
      }
      if (url === `${BROKER}/topup`) {
        topupCount += 1;
        return Promise.resolve(
          new Response(null, {
            status: 409,
            headers: {
              "Livepeer-Error": topupCount === 1 ? "recipient_rotated" : "rebind_refused",
            },
          }),
        );
      }
      return Promise.resolve(json({}, 404));
    };
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    const runner = new SessionRunner({
      client,
      handle: handle(),
      fetch: fetchImpl,
      onWinddownWarning: (event) => {
        warnings.push(event);
      },
    });
    await runner.start();
    await runner.onBalance(balance({ status: "low" }));
    expect(warnings.map((event) => event.reason)).toEqual(["payment_unrecoverable"]);
    expect(refillCount).toBe(2);
    expect(topupCount).toBe(2);
  });

  it("fails closed on descriptor mismatch", async () => {
    const fetchImpl: typeof fetch = () => Promise.resolve(json(openResponse("wrong/v1")));
    const client = new OpenClearinghouseClient({
      baseUrl: LOC,
      apiKey: "pymth_test",
      fetch: fetchImpl,
    });
    await expect(
      new SessionRunner({ client, handle: handle(), fetch: fetchImpl }).start(),
    ).rejects.toThrow(/descriptor/);
  });
});
