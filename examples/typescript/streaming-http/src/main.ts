/**
 * Streaming session with HTTP topup (live-session-remote-runner@v0).
 *
 *     OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
 *     OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
 *     pnpm --filter @livepeer/example-streaming-http start
 *
 * For HTTP-topup modes, the broker doesn't push balance-low frames over
 * a WebSocket — the customer's media plane observes balance-low
 * out-of-band and routes the signal in via runner.onBalanceLow(). The
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
    throw new Error("set OPEN_CLEARINGHOUSE_URL and OPEN_CLEARINGHOUSE_API_KEY");
  }

  const client = new OpenClearinghouseClient({ baseUrl, apiKey });

  const handle = await client.openSession({
    capability: "livepeer:remote-runner",
    offering: "live-session-remote-runner",
    estimatedRunwayUnits: 1000,
    maxTotalUnits: 10000,
  });
  console.log(`session opened: ${handle.sessionId} (mode=${handle.mode})`);

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
    await runner.onBalanceLow({ observed_consumed_units: 500 });

    const result = await runner.close({ actualUnits: 750, outcome: "complete" });
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
