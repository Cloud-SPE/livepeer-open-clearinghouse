/**
 * End-to-end example: submit a job via the handoff-mode SDK.
 *
 *     OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
 *     OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
 *     pnpm --filter @livepeer/example-one-shot-job start
 *
 * The SDK handles the full handoff dance: opens a job via POST /v1/jobs
 * (which mints a payment envelope), calls the broker directly with the
 * envelope as Livepeer-Payment, reads the broker's Livepeer-Work-Units
 * header from the response, and posts settle back to LOC.
 */

import {
  InsufficientCredit,
  NoRouteAvailable,
  OpenClearinghouseClient,
  OpenClearinghouseError,
  RateLimited,
} from "@livepeer/open-clearinghouse-sdk";

async function chat(prompt: string): Promise<void> {
  const baseUrl = process.env.OPEN_CLEARINGHOUSE_URL;
  const apiKey = process.env.OPEN_CLEARINGHOUSE_API_KEY;
  if (!baseUrl || !apiKey) {
    throw new Error("set OPEN_CLEARINGHOUSE_URL and OPEN_CLEARINGHOUSE_API_KEY");
  }

  const client = new OpenClearinghouseClient({ baseUrl, apiKey });

  try {
    const result = await client.submitJob({
      capability: "openai:chat-completions",
      offering: "gpt-oss-20b",
      // Best-guess input tokens; broker reports actual via Livepeer-Work-Units.
      estimatedUnits: 200,
      // Worst-case ceiling — LOC encumbers this much up front.
      maxTotalUnits: 2000,
      body: {
        messages: [{ role: "user", content: prompt }],
        max_tokens: 500,
      },
    });

    if (result.status === 200) {
      console.log("==== broker response ====");
      console.log(result.body);
      console.log();
      console.log("==== final accounting ====");
      console.log(`actual units consumed: ${result.actualUnits}`);
      console.log(`billed:                ${result.billedValueWei} wei`);
      console.log(`refund:                ${result.refundWei} wei`);
      console.log(`outcome:               ${result.outcome}`);
      if (result.capStatus.will_refuse_next_refill) {
        console.log(
          `⚠️  cap warning: ${result.capStatus.winddown_reason} — another job at this size may be refused`,
        );
      }
    } else {
      console.log(`broker returned ${result.status}`);
      console.log(result.body);
    }
  } catch (exc) {
    if (exc instanceof InsufficientCredit) {
      console.log("not enough credit:", exc);
    } else if (exc instanceof NoRouteAvailable) {
      console.log("no orchestrator advertising this capability/offering");
    } else if (exc instanceof RateLimited) {
      console.log("rate limited");
    } else if (exc instanceof OpenClearinghouseError) {
      console.log("loc error:", exc.code, "-", exc.message);
    } else {
      throw exc;
    }
  }
}

chat("explain handoff mode in two sentences").catch((err) => {
  console.error(err);
  process.exit(1);
});
