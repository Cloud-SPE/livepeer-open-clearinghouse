/**
 * Extensible paid-session/v1 session with authoritative HTTP top-up.
 *
 *     OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
 *     OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
 *     pnpm --filter @livepeer/example-streaming-http start
 *
 * The customer's media plane observes the broker's normative balance
 * object and routes it in via runner.onBalance(). The
 * runner then asks LOC for a refill and POSTs it to the broker's
 * control.topup_url.
 */

import {
  OpenClearinghouseClient,
  OpenClearinghouseError,
  SessionRunner,
} from "@livepeer/open-clearinghouse-sdk";

async function main(): Promise<void> {
  const baseUrl = process.env.OPEN_CLEARINGHOUSE_URL;
  const apiKey = process.env.OPEN_CLEARINGHOUSE_API_KEY;
  if (!baseUrl || !apiKey) {
    throw new Error(
      "set OPEN_CLEARINGHOUSE_URL and OPEN_CLEARINGHOUSE_API_KEY",
    );
  }

  const client = new OpenClearinghouseClient({ baseUrl, apiKey });

  const handle = await client.openSession({
    capability: "livepeer:remote-runner",
    offering: "live-session-remote-runner",
    descriptorSchema: "livepeer.session.remote-runner/v1",
    estimatedRunwayUnits: 1000,
    maxTotalUnits: 10000,
  });
  console.log(
    `session opened: ${handle.sessionId} (protocol=${handle.protocol})`,
  );

  const runner = new SessionRunner({
    client,
    handle,
    onRefillSucceeded: (event) => {
      console.log(`refill #${event.refillSeq}: +${event.fundedValueWei} wei`);
    },
    onRefillRefused: (event) => {
      console.log(`refill refused: ${event.error?.code ?? "unknown"}`);
    },
    onWinddownWarning: (event) => {
      console.log(`winddown: ${event.reason}`);
    },
  });

  try {
    await runner.start();

    // Customer-driven refill. In production this fires when the media
    // plane observes balance-low on the runner channel.
    await runner.onBalance({
      status: "low",
      claimed_units: 500,
      debited_units: 500,
      unit: "session_second",
      runway_units: 100,
      runway_seconds_estimate: 100,
      will_refuse_next_refill: false,
    });

    const result = await runner.close({
      actualUnits: 750,
      outcome: "complete",
    });
    console.log("==== final settlement ====");
    console.log(`outcome: ${result.outcome}`);
    console.log(`billed:  ${result.billed_value_wei} wei`);
    console.log(`refund:  ${result.refund_wei} wei`);
  } catch (exc) {
    if (exc instanceof OpenClearinghouseError) {
      console.log("loc error:", exc.code, "-", exc.message);
    } else {
      throw exc;
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
