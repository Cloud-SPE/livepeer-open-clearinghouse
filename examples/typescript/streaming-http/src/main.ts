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
import { secp256k1 } from "@noble/curves/secp256k1.js";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { bytesToHex, concatBytes, utf8ToBytes } from "@noble/hashes/utils.js";

/**
 * Caller key + Livepeer-Caller-Proof signer. The SDK never holds the caller's
 * private key, so signing lives here. This example uses a fresh ephemeral key
 * per run; production callers keep and reuse their own key.
 *
 * proof = base64(R || S || V) of an EIP-191 personal-sign over
 * keccak256("livepeer-invocation-proof/v1\0" || authorization).
 */
function callerSigner(privateKey = secp256k1.utils.randomSecretKey()) {
  const callerPublicKey = bytesToHex(secp256k1.getPublicKey(privateKey, true));
  const signCallerProof = (authorization: Uint8Array): string => {
    const digest = keccak_256(
      concatBytes(utf8ToBytes("livepeer-invocation-proof/v1\x00"), authorization),
    );
    const msgHash = keccak_256(
      concatBytes(utf8ToBytes("\x19Ethereum Signed Message:\n32"), digest),
    );
    // noble's "recovered" format is recovery(1) || R(32) || S(32).
    const sig = secp256k1.sign(msgHash, privateKey, { prehash: false, format: "recovered" });
    const rsv = concatBytes(sig.subarray(1), Uint8Array.of(27 + sig[0]!));
    return Buffer.from(rsv).toString("base64");
  };
  return { callerPublicKey, signCallerProof };
}

async function main(): Promise<void> {
  const baseUrl = process.env.OPEN_CLEARINGHOUSE_URL;
  const apiKey = process.env.OPEN_CLEARINGHOUSE_API_KEY;
  if (!baseUrl || !apiKey) {
    throw new Error(
      "set OPEN_CLEARINGHOUSE_URL and OPEN_CLEARINGHOUSE_API_KEY",
    );
  }

  const client = new OpenClearinghouseClient({ baseUrl, apiKey });
  const { callerPublicKey, signCallerProof } = callerSigner();

  const handle = await client.openSession({
    capability: "livepeer:remote-runner",
    offering: "live-session-remote-runner",
    descriptorSchema: "livepeer.session.remote-runner/v1",
    estimatedRunwayUnits: 1000,
    maxTotalUnits: 10000,
    callerPublicKey,
    signCallerProof,
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
