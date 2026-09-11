/**
 * paid-session/v1 session with an optional broker events WebSocket.
 *
 *     OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
 *     OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
 *     pnpm --filter @livepeer/example-streaming-ws start
 *
 * SessionRunner connects to the broker over a control WebSocket. When
 * the broker pushes a Livepeer-Balance-Low frame, the runner asks LOC
 * for a refill and delivers it back as a session.topup frame — the
 * onRefillSucceeded callback fires on each successful top-up.
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
    capability: "livepeer:live-video-control",
    offering: "session-control-plus-media",
    descriptorSchema: "livepeer.session.video-control/v1",
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
    // Hold the session briefly so the broker has a chance to push at
    // least one Livepeer-Balance-Low frame. Production code would
    // drive its own media plane on top of this WS rather than sleeping.
    await new Promise((r) => setTimeout(r, 3000));

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
