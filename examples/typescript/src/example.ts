/**
 * End-to-end example: mint a payment, simulate sending to an orch,
 * reconcile usage.
 *
 *     PYMTHOUSE_URL=http://localhost:8000 \
 *     PYMTHOUSE_API_KEY=pymth_live_... \
 *     pnpm example
 */

import { randomUUID } from "node:crypto";
import { InsufficientCredit, NoRouteAvailable, PymtHouseClient, RateLimited } from "./index.js";

async function main(): Promise<void> {
  const baseUrl = requireEnv("PYMTHOUSE_URL");
  const apiKey = requireEnv("PYMTHOUSE_API_KEY");

  const ph = new PymtHouseClient({ baseUrl, apiKey });

  // 1. Pick an offering
  const caps = await ph.listCapabilities();
  const chatCap = caps.find((c) => c.name === "openai:chat-completions");
  if (!chatCap || chatCap.offerings.length === 0) {
    throw new Error("no chat-completions offering advertised right now");
  }
  const offering = chatCap.offerings[0]!.id;
  console.log(`using offering: ${offering}`);

  // 2. Mint with a budget of ~1000 tokens; one Idempotency-Key per logical request
  const idem = randomUUID();
  let mint;
  try {
    mint = await ph.mintPayment({
      capability: "openai:chat-completions",
      offering,
      workUnits: 1000,
      idempotencyKey: idem,
    });
  } catch (err) {
    if (err instanceof InsufficientCredit) {
      console.error("need topup:", err.details);
      return;
    }
    if (err instanceof NoRouteAvailable) {
      console.error("no orch advertising this offering — try another");
      return;
    }
    if (err instanceof RateLimited) {
      console.error(`rate limited; retry in ${err.retryAfterSeconds}s`);
      return;
    }
    throw err;
  }
  console.log(`minted: work_id=${mint.work_id.slice(0, 16)}… ev=${mint.expected_value_wei}`);
  console.log(`orch=${mint.recipient_eth_address}`);
  console.log(`Livepeer-Payment header (truncated): ${mint.payment_bytes.slice(0, 48)}…`);

  // 3. Real code would POST to the orch's URL here. We simulate that the orch
  //    responded and consumed 873 tokens.
  const actualTokens = 873;

  // 4. Reconcile
  const result = await ph.reportUsage({
    paymentId: mint.payment_id,
    actualWorkUnits: actualTokens,
    idempotencyKey: idem,
  });
  console.log(
    `refunded ${result.refunded_wei} wei; new balance ${result.new_balance_wei} wei`,
  );
}

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`missing required env var: ${name}`);
  return v;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
