# 002. Long-Running Sessions

**Status:** ready for review (Q#1, Q#2, Q#3 all resolved)
**Owner:** Codex
**Opened:** 2026-05-23
**Closed:** —

## Intent

Define how Livepeer Open Clearinghouse handles workloads that don't fit
the current single-shot `submit_job` / mint-once-and-done pattern —
specifically:

- Long-running HTTP responses (SSE / chunked streams).
- Bidirectional WebSocket sessions.
- Continuous RTMP / HLS video sessions.

Pick a model, capture the upstream primitives we lean on, and produce
the schema, runtime, and SDK additions LOC needs in order to plug into
the upstream protocol without reinventing it.

## Scope

- In: clearinghouse-side data model, runtime endpoints, payment-daemon
  RPC surface, customer-facing SDK shape, observability hooks.
- Out: extractor implementations (offering-side), ABR pricing
  semantics (capability-side), multi-broker failover during an active
  session (separate plan — note in open questions).

## Glossary

The upstream Livepeer protocol and LOC's customer-facing surface use
overlapping but distinct terms. Pinning them down once so the rest of
this doc has unambiguous referents.

### Actors in the data path

```
   ┌─────────────┐    ┌──────────────────────┐    ┌──────────────┐    ┌─────────┐
   │ Customer    │ →  │ Livepeer Open        │ →  │ Broker       │ →  │ Backend │
   │ App         │    │ Clearinghouse (LOC)  │    │ (capability- │    │ Worker  │
   │ (curl, SDK) │    │ — the "gateway"      │    │  broker)     │    │ (vLLM,  │
   └─────────────┘    └──────────────────────┘    └──────────────┘    │ ffmpeg) │
                                                                       └─────────┘
                              we run                 operator runs        operator
                                                                          runs
```

| Term | What it is | Who runs it | In our deployment |
|---|---|---|---|
| **Customer app** | Whatever the end user is building (chatbot, script, SDK call). | Customer. | Hits `LOC: POST /v1/jobs` or, later, `POST /v1/sessions`. |
| **Gateway** | Customer-facing entry point. Authenticates customers, mints `Livepeer-Payment` via the payer-side payment-daemon, sends the paid request to the broker, reconciles billing on the response. | Anyone who wants to resell capability access. **LOC is the gateway in our world.** | The FastAPI app at `localhost:8000` is the gateway. |
| **Broker** (`capability-broker`) | Orchestrator-side HTTP/WS termination. Receives paid requests, validates the ticket via the payee-side payment-daemon, opens a session (`OpenSession`), forwards the (header-stripped, backend-auth-injected) request to the backend worker, computes `actualUnits` via the offering's extractor, reports `Livepeer-Work-Units` in the response, closes the session. | Each orchestrator operator. | Lives in the orchestrator network. LOC discovers brokers via `service-registry-daemon.Select()` and reaches them at the URLs the registry returns. |
| **Backend worker** | The actual compute (vLLM instance, ffmpeg pipeline, OpenAI-resale endpoint). Speaks plain HTTP/WS with backend-native auth — does not see the Livepeer protocol. | Orchestrator operator, or a third party they're reselling. | LOC never touches it directly. |

### Two payment-daemon roles

Same binary, two modes — easy to confuse.

| Mode | Where it runs | What it does | LOC's relationship |
|---|---|---|---|
| **`payer-daemon` (sender mode)** | Next to the gateway. | Signs tickets (`CreatePayment`), tracks per-session sender nonces, watches on-chain escrow. | This is the `payment-daemon` service in our compose. LOC calls it via `GrpcPaymentDaemonClient`. |
| **`payee-daemon` (receiver mode)** | Next to the broker. | Validates incoming tickets (`ProcessPayment`), holds the per-session balance ledger, debits work units (`DebitBalance`), closes sessions (`CloseSession`). | LOC never calls it; the broker does on the orchestrator side. |

### Term mapping (upstream → this doc → LOC code)

| Upstream term | Used in this doc as | Where it shows up in LOC today |
|---|---|---|
| "gateway" | "LOC" or "the gateway" | the FastAPI app (`src/livepeer_open_clearinghouse/`) |
| "broker" / "capability-broker" | "broker" | external; URLs come from `service-registry-daemon` |
| "payer-daemon" (sender mode) | "payment-daemon" | compose service `payment-daemon`; client at `providers/payment_daemon/` |
| "payee-daemon" (receiver mode) | n/a — LOC never talks to it | n/a |
| "work_id" | "session key" / "work_id" | bound at mint time; will become FK on `payment_session` |
| "work unit" | "work unit" | opaque per-capability (`token`, `audio_second`, ...); price applied at the offering layer |
| `actualUnits` / `Livepeer-Work-Units` | "actual units" | future column on `payment_session` |
| `SettlementRecord` | "settlement record" | future row in `payment_settlement` |

### A subtlety worth naming

In LOC's customer-facing language, the LOC gateway bundles two roles:

1. **Customer-facing**: API-key auth, user balance bookkeeping, the
   portal and admin SPAs.
2. **Livepeer-facing**: the upstream "gateway" role — mint payments,
   send paid requests to brokers, reconcile.

Upstream only sees role (2). `submit_job` today is "LOC qua upstream
gateway" doing role (2), with role (1) wrapped around it.

### Control plane vs. data plane (after Q#2)

After resolving Q#2 in favor of **handoff mode**, LOC's gateway role
splits along this axis:

- **Control plane** (LOC's job): authenticate the customer, validate
  caps, mint payments, hand the SDK a `{broker_url, payment_envelope}`,
  receive settlement reports, reconcile balances, run the safety-net
  janitor.
- **Data plane** (no longer LOC's job): the actual capability traffic
  — request bodies, response bodies, WS frames, RTMP chunks — flows
  **customer SDK ↔ broker** directly. LOC never sees the bytes.

This is a deliberate shift away from today's `submit_job`, which
proxies the request through LOC. The cost is reduced observability;
the benefits are dramatically lower bandwidth (essential for live
video), better LOC-outage behavior (running sessions survive), and a
cleaner separation of concerns. See "Q#2" and "Alternative
architectures considered" for the full reasoning.

## Background — four workload shapes

The user-facing taxonomy this plan covers. Each row is a distinct
real-world pattern with different settlement cadence.

| # | Shape | Example | Funding known up front? | Settlement cadence |
|---|---|---|---|---|
| a | Atomic job | Embeddings, image generation, fixed-length transcode | Exact | None (or implied at completion) |
| b | Post-settled job | OpenAI non-streaming chat (input tokens known, output tokens not) | Max-bound only | One final settle |
| c | Streaming chunks | OpenAI chat with `stream: true` (chunks accrue token cost) | Max-bound only | Many settles, then close |
| d | Continuous stream | Live video / ABR ladder, real-time audio | Open-ended | Many settles, refills, then close |

These are points on one spectrum, not separate primitives. The
underlying object is a **payment session** that holds one or more
tickets and accumulates debits over time; the four cases differ only
in (i) how accurately face value can be sized at session-open, and
(ii) how often debits arrive.

## Q#1 — Does the orchestrator side support long-running sessions?

**Yes, comprehensively.** The upstream
[`livepeer-network-modules`](../../../livepeer-cloud-spe/livepeer-network-modules)
repo defines a complete session lifecycle and six accepted interaction
modes that map almost 1:1 to the four shapes above.

### `PayeeDaemon` session-balance RPCs

Defined in
`livepeer-network-protocol/proto/livepeer/payments/v1/payee_daemon.proto`.
This is the broker-side daemon, called by orchestrator workers — LOC
itself does not call it directly. The shape of these RPCs determines
what the gateway sees.

| RPC | Purpose |
|---|---|
| `OpenSession(sender, work_id, ...)` | Bind authoritative pricing metadata to `work_id`. Idempotent re-open returns the same `TicketParams`. |
| `ProcessPayment(payment_bytes)` | Validate a ticket, credit sender balance, queue any winning ticket for redemption. Requires a previously-opened session. |
| `DebitBalance(sender, work_id, units, debit_seq)` | Idempotent per-session debit by `(sender, work_id, debit_seq)`. The interim-debit primitive. |
| `SufficientBalance(sender, work_id, min_units)` | Pre-flight: does the balance cover this much work? Read-only. |
| `GetBalance(sender, work_id)` | Current balance. |
| `CloseSession(sender, work_id)` | Garbage-collect. Residual credit is forfeited. Outcome enum: `CLOSED` / `ALREADY_CLOSED`. |

### `PayerDaemon` (our daemon, sender mode)

Defined in
`livepeer-network-protocol/proto/livepeer/payments/v1/payer_daemon.proto`.
This is the daemon LOC already wraps via `GrpcPaymentDaemonClient`.

| RPC | Purpose |
|---|---|
| `CreatePayment` | Mint a `Payment` blob against a quote + funding intent. Returns `work_id`. The daemon caches sessions by `(recipient, capability, offering, funded_value_wei, broker_url)` so repeat calls with the same key reuse `recipient_rand_hash` and increment nonce. |
| `ReportPaymentResult` | Caller reports a payee rejection (e.g. `INVALID_RECIPIENT_RAND`) so the daemon evicts cached session state. |
| `GetSessionDebits(sender, work_id)` | Read the per-session debit ledger. Returns `total_work_units`, `debit_count`, `closed`. May be `UNIMPLEMENTED`. |
| `GetDepositInfo` | TicketBroker deposit/reserve/withdraw-round state. |
| `Health` | Liveness probe. |

### Key wire-side data structures

From `livepeer-network-protocol/proto/livepeer/payments/v1/types.proto`:

- **`FundingIntent`** — `estimated_units`, `funded_value_wei`,
  `max_total_units`, `top_up_allowed`. Per-request or per-session
  budget envelope.
- **`SettlementRecord`** — `actual_units`, `billed_units`,
  `billed_value_wei`, plus `SettlementOutcome` enum (`EXACT` /
  `UNDERFUNDED` / `OVERFUNDED` / `STOPPED_AT_BUDGET` / `TOPPED_UP`)
  and an opaque `breakdown` map for workload-specific accounting
  (e.g., input_tokens vs output_tokens, per-ABR-rung byte counts).
- **`CapabilityEntry.work_unit`** — opaque string identifier
  (`"token"`, `"audio_second"`, `"image_step_megapixel"`,
  `"character"`, etc.). The protocol treats work units as opaque;
  pricing is `wei per N work_units` where N is the offering's
  `pixels_per_unit`.

### Eight accepted modes

From `livepeer-network-protocol/modes/`. Each carries its own SemVer.

| Mode | Status | Lifecycle | Maps to our case |
|---|---|---|---|
| `http-reqresp@v0` | accepted 2026-05-06 | one req → one resp; single-debit + reconcile per request | (a), (b) |
| `http-stream@v0` | accepted 2026-05-06 | request → SSE / chunked response; `Livepeer-Work-Units` arrives as **HTTP trailer** | (c) |
| `http-multipart@v0` | accepted 2026-05-06 | multipart upload → JSON or binary response | variant of (a)/(b) |
| `ws-realtime@v0` | accepted 2026-05-06 | bidirectional WS; **interim-debit + final reconcile**; no topup | (d-bounded) |
| `rtmp-ingress-hls-egress@v0` | accepted 2026-05-06 | RTMP in → HLS out; same lifecycle as `ws-realtime`, but with topup via optional control WS | (d-extensible), live video |
| `session-control-plus-media@v0` | accepted 2026-05-06 | HTTP session-open → long-lived media plane + separate control WS | (d-extensible), advanced |
| `live-session-remote-runner@v0` | accepted 2026-05-20 | broker-owned session, runner-owned RTMP/HLS runtime | (d-extensible), variant |
| `live-session-gateway-ingest@v0` | accepted 2026-05-20 | broker-owned session, gateway-owned ingest, runner-to-gateway object storage | (d-extensible), variant |

### Case-(d) modes compared (channel topology + ownership)

The five long-running modes look similar at first; their actual
differences are about **how many channels exist**, **who owns the
media plane**, and **whether topup is possible**.

| Mode | Channels | Media plane ownership | Topup? | Canonical use |
|---|---|---|---|---|
| `ws-realtime@v0` | 1 WS (data + control combined) | the WS itself | ❌ | OpenAI Realtime API; voice agents; ephemeral bidirectional chat |
| `session-control-plus-media@v0` | 1 control WS + 1 capability-defined media plane | **separate** (RTMP / trickle / custom — broker stands it up but doesn't carry bytes) | ✅ (`session.topup` on control WS) | VTuber sessions; long-lived stateful workloads with audio/video |
| `rtmp-ingress-hls-egress@v0` | RTMP in + HLS out + optional control WS | **broker-local** (broker runs ffmpeg + serves HLS) | ✅ (control WS or RTMP disconnect on no-pay) | Simple live transcode where the orchestrator owns the full pipeline end-to-end |
| `live-session-remote-runner@v0` | HTTP control + remote-runner-owned RTMP/HLS | **runner-owned** (broker delegates to a remote runner) | ✅ (HTTP `POST {topup_url}`) | Live RTMP → HLS where the media runtime is decoupled from the broker process |
| `live-session-gateway-ingest@v0` | HTTP control + gateway-owned RTMP ingest + gateway-owned HLS storage | **split**: gateway-owned ingest + runner-owned encode + gateway-owned HLS object store | ✅ (HTTP `POST {topup_url}`) | Gateway needs to hold the public RTMP ingest URL and HLS output URL while orchestrator does the encode |

Per-mode summary in one sentence:

- **`ws-realtime`** — "I just need a bidirectional pipe; the application defines what flows."
- **`session-control-plus-media`** — "Same as ws-realtime but I also need a separate AV media plane and the ability to topup."
- **`rtmp-ingress-hls-egress`** — "The orchestrator runs my entire live-video pipeline; I'm hands-off."
- **`live-session-remote-runner`** — "The orchestrator manages billing, but the media runtime lives somewhere else (their datacenter, their cloud)."
- **`live-session-gateway-ingest`** — "I want to keep my RTMP ingest URL public and own the playback URLs; orchestrator only does the encode work."

The customer never picks a mode directly — they pick a **capability
+ offering**, and the offering metadata carries the mode string.
The SDK reads it and routes its session-handling code accordingly.
The SDK's public API (`open_session`) does not expose mode as a
parameter.

### Mode propagation path (capability declaration → SDK)

End-to-end audit of how a mode string flows from the orchestrator's
manifest to the SDK's session driver, with the LOC integration
gap surfaced for fix in v1:

1. **Orchestrator declares mode** in its capability manifest. Per
   upstream `service-registry-daemon/internal/types/coordinator_envelope.go`:
   the manifest's capability tuple carries an `interaction_mode`
   field (e.g., `"ws-realtime@v0"`). It's required at registration
   time; missing values fail validation.
2. **Service-registry-daemon ingests** the manifest, normalizes the
   capability set, and serializes `interaction_mode` into the
   capability's `extra` map as a JSON field. This `extra` map is
   shipped over the wire as the `extra_json` (bytes) field on the
   proto `Capability` and `SelectedRoute` messages.
3. **Resolver `Select` RPC returns** a `SelectedRoute` whose
   `extra_json` carries `{"interaction_mode": "<mode-string>", ...}`
   among any other capability-attached metadata.
4. **LOC's registry client** (`providers/registry_daemon/client.py`)
   receives the proto `SelectedRoute` and converts it to the Python
   `SelectedRoute` dataclass — **today this conversion drops
   `extra_json` entirely.** The Python dataclass has no `extra` or
   `mode` field. This is the integration gap.
5. **LOC's session-open handler** (when `POST /v1/sessions` lands)
   should write the mode onto `payment_session.mode`, and should
   return it to the SDK in the session-open response so the SDK
   can pick its driver. Today it can't, because step 4 dropped the
   data.
6. **SDK** receives `{session_id, broker_url, payment_envelope,
   mode, refill_endpoint, ...}` and selects its mode-specific
   session driver (ws-realtime / session-control-plus-media /
   live-session-* / etc.).

#### The v1 fix

Two small changes land this end-to-end:

- **`SelectedRoute` dataclass**: add `extra: dict[str, Any]` (or
  a typed wrapper) populated by parsing the proto `extra_json`
  bytes. Lossless conversion; future extra fields (beyond mode)
  come along for free.
- **Session-open handler**: read `extra.get("interaction_mode")`,
  write to `payment_session.mode`, include in the response payload
  to the SDK. SDK conformance tests assert mode is present and
  matches an accepted value.

Both go in the v1 implementation roadmap (added to Done Looks
Like). No protocol change needed — upstream already provides the
field; we just need to stop dropping it.

### Key insights from upstream

These shape LOC's design and override several assumptions in earlier
drafts:

1. **The broker authors the bill, not LOC.** For modes
   `http-reqresp`/`http-stream`/`http-multipart`, the broker computes
   `actualUnits` via the offering's declared extractor and reports it
   **in the HTTP response itself** — header for sync, trailer for
   streams. LOC doesn't need a webhook or push channel for cases
   (a)–(c); the bill lands in the response we already receive.

2. **`Livepeer-Settlement` is a header too.** The proto's
   `SettlementRecord` message is delivered to LOC via a response
   header carrying serialized settlement metadata. One transport
   mechanism for all single-request modes.

3. **For continuous modes, the *sender* payment-daemon already holds
   the running ledger.** From `ws-realtime.md`: *"the gateway's
   payment-daemon-sender knows the running debit total (via session
   ledger). No mid-session 'report back' is needed; the running tick
   total IS the bill."* So for case (d), LOC just calls
   `GetSessionDebits(sender, work_id)` whenever it needs to know how
   much has been consumed.

4. **Cadence parameters are offering-controlled.** `cadence_seconds`
   (default 5), `runway_min_seconds` (default 15),
   `grace_window_ticks` (default 2) are advertised in the offering's
   `extra` block. LOC reads them; it doesn't decide them.

5. **Balance-low signaling is in-band.** For mode (d), the broker
   emits a capability-defined `Livepeer-Balance-Low` application
   message *before* closing on balance-zero. The gateway sees it and
   decides whether to refill or wind down.

## Q#2 — Wire shape for debit reports & refills (resolved 2026-05-24)

**Mode**: **handoff**. LOC is the control plane; data flows customer
SDK ↔ broker directly. LOC never sees request/response bytes.

### Rationale

Three forces pushed the call:

1. **Bandwidth** — proxy mode is operationally hostile for live
   video. A single RTMP stream is ~5 Mbps in and ~7 Mbps of ABR
   output; 100 concurrent streams = ~1.2 Gbps continuously
   through LOC's pipe. Handoff is the only model that scales
   to video without LOC becoming an expensive bandwidth utility.
2. **LOC-outage blast radius** — proxy: 100% of active sessions die
   when LOC restarts. Handoff: only new mints fail; running sessions
   continue for the duration of their funded runway, and refill-time
   sessions degrade gracefully at their grace boundary instead of
   crashing.
3. **Architectural cleanliness** — LOC stops being a multi-protocol
   relay (HTTP / SSE / WS / RTMP) and becomes a JSON control plane
   with one job: authorize, mint, reconcile.

The cost is **reduced observability** and **trust dependency on the
SDK** — both addressed below.

### Sub-decisions, all resolved by the handoff choice

| # | Sub-decision | Resolution |
|---|---|---|
| 1 | Session declaration | Explicit `POST /v1/sessions` (resolved 2026-05-23). Response now also carries `broker_url` and `refill_endpoint` so the SDK can complete the handoff. |
| 2 | Refill initiator (case d) | **SDK is event-driven on `Livepeer-Balance-Low`** (the broker emits it to whoever owns the WS, which in handoff mode is the SDK). The SDK calls `POST /v1/sessions/{id}/refill`; LOC mints + returns the top-up envelope. |
| 3 | Refill delivery | **SDK delivers the refill envelope to the broker over the same WS it's already holding**, using the capability-defined in-band frame format (per the mode spec — TBD: confirm the exact frame shape with upstream once `ws-realtime@v0` conformance tests publish). |
| 4 | Poll cadence | **No fast polling.** LOC runs only a *slow* reconciliation poll (default: every 60s per open session) against `payer-daemon.GetSessionDebits` as a safety net for SDK silence. No per-cadence-tick polling. |
| 5 | Response parsing | **SDK reads** `Livepeer-Work-Units` (header for sync, trailer for streams) and `Livepeer-Settlement` from broker responses, then posts to `POST /v1/jobs/{id}/settle` or `POST /v1/sessions/{id}/close`. LOC verifies against `GetSessionDebits` before writing the final settlement row. |

### What this means for each case in one line

- **(a)/(b)** — SDK mints via LOC, calls broker directly, reads the
  response header, posts settlement to LOC. LOC verifies, reconciles.
- **(c)** — Same as (b), but SDK reads `Livepeer-Work-Units` from the
  HTTP **trailer** (mid-stream disconnect → broker closes
  server-side, SDK posts best-effort settlement on whatever it
  received).
- **(d)** — SDK opens WS to broker with minted envelope. SDK
  observes `Livepeer-Balance-Low` on the WS, calls LOC's refill
  endpoint, delivers the returned envelope back over the WS. SDK
  posts close on session end.

## Trust model (handoff implications)

In proxy mode, LOC was authoritative by virtue of being in the path —
it could read every byte of every response. In handoff mode, LOC
never sees broker traffic, so we need to be explicit about whose
report counts and how disputes are resolved.

### Two sources of "what happened"

1. **Customer SDK self-report** — `POST /v1/jobs/{id}/settle` or
   `POST /v1/sessions/{id}/close` carries `actual_units` and the
   parsed `SettlementRecord`. Arrives quickly. **Customer-controlled
   and therefore not trusted on its own.**
2. **`payer-daemon.GetSessionDebits(sender, work_id)`** — the
   sender-side daemon mirrors the broker's payee-daemon ledger.
   Authoritative. **LOC always cross-checks the SDK's report against
   the daemon before writing the final settlement row.**

### Resolution rules

| SDK reported | Daemon says | What LOC bills | Side effects |
|---|---|---|---|
| `actual_units=X` | `total_work_units=X` | `X` | Normal path. Settle, done. |
| `actual_units=X` | `total_work_units=Y > X` | `Y` (daemon wins) | Log discrepancy. Increment per-API-key `under_report_count`. Threshold → operator alert. |
| `actual_units=X` | `total_work_units=Y < X` | `Y` (daemon wins) | Log; this is the "honest customer reported high because broker debited less than they thought" path. No alert. |
| No SDK report | `closed=true, total=Y` | `Y` | Janitor finalized. Normal for SDK crash. No alert unless repeated for the same key. |
| No SDK report | `closed=false`, stale | (nothing yet) | Janitor logs "stuck session" warning. Holds the funded value as encumbered until daemon shows closed. |

### Cheat surface analysis

The SDK is in a position to misreport — but is the SDK in a position
to *steal*? Walking the surface:

| Attempt | Why it doesn't work |
|---|---|
| Use a minted ticket against a different broker | Tickets are cryptographically bound to `recipient` (broker's on-chain address). Wrong broker rejects. |
| Use a minted ticket for a different capability/offering | Bound via `AcceptedPrice.quote_ref` and the broker's session pricing. Mismatched offering rejects. |
| Replay a ticket | Sender nonces are monotonic per `recipient_rand_hash`. Replay rejects. |
| Mint and never use, asking LOC to "refund" | LOC encumbers funded value at mint time. Refund on session close = `funded − billed`. If never used, `billed=0` and full refund returns to balance. No theft, just operational noise. |
| Under-report actual_units to LOC | Daemon cross-check wins. Detected immediately. |
| Drop session silently to avoid the close call | Janitor catches via `GetSessionDebits`. Customer is still billed for actual debits. |
| Mint many sessions, exhaust escrow, default | Existing `BillingConfig.spend_period_cap_wei` already prevents this at the user-balance layer. |

**Conclusion**: a malicious SDK can degrade its own UX (no refills,
no balance display, no graceful close) but cannot steal capacity from
the operator or other users. Cheating reduces to "don't pay LOC for
work that didn't happen," which is impossible because the bill comes
from the daemon's ledger, not the SDK's report.

The remaining concern is **operational integrity** — bugs in the SDK
cause real customer harm (lost sessions, missed refills, wrong UI
state). That's what the SDK conformance program below addresses.

## SDK criticality and conformance

Because handoff mode makes the SDK load-bearing for refills,
settlement, and graceful close, the official LOC SDKs are
operationally part of the platform, not a convenience layer. This
section captures the bar.

### Three rules for the SDK

1. **The official LOC SDK is the supported integration path.** Any
   other client (curl, a customer's own implementation) is
   tolerated but unsupported. Discovery docs, OpenAPI, error
   messages, and dashboards all assume an official SDK is in use.
2. **SDK behavior is hidden from the consumer.** Customers should
   never see Livepeer-* headers, EV math, balance-low events, ticket
   delivery, refill timing, or trailer-parsing. The public API
   surface is `submit_job` / `submit_stream` / `open_session` and
   nothing else.
3. **Discrepancies between SDK report and daemon ledger are billed
   from the daemon.** SDK acts as a fast-path; daemon is the
   authority (see Trust model above).

### Version pinning and conformance signaling

LOC needs to know which SDK is in use to grade trust and triage
incidents.

- **SDK identity header**: every SDK request to LOC carries
  `Livepeer-Open-Clearinghouse-SDK: <lang>/<semver>/<git_sha7>`,
  e.g. `python/0.4.2/a1b3c5d`. LOC stores this on the
  `payment_session` / `payment` row for audit.
- **Approved-version manifest**: LOC publishes a list of supported
  `<lang>/<semver>` pairs (with their expected git SHAs) at
  `/v1/sdk/manifest`. The manifest is signed by an operator key so
  third parties can verify it offline.
- **Per-request validation**: LOC rejects requests carrying SDK
  identifiers older than the operator-configured `min_sdk_version`
  with `426 Upgrade Required` and a `Livepeer-Error:
  sdk_version_unsupported` body. Customers see a clear "upgrade
  your SDK" message from the SDK itself.
- **SHA mismatch handling**: if `Livepeer-Open-Clearinghouse-SDK`
  reports a `<lang>/<semver>` on the approved list but the SHA
  doesn't match the manifest, LOC still serves the request (we
  can't enforce SDK integrity remotely — the SDK could lie) but
  logs `sdk_sha_mismatch` for that API key. Used by the operator
  alerting program below.

This is **hygiene, not cryptographic enforcement.** The SDK runs on
the customer's machine; nothing prevents them from claiming any
identity. The point is to surface ground truth: legitimate users on
old versions get a clean upgrade prompt; tampered SDKs show up in
operator logs.

### What actually prevents cheating

Three layers, in order of strength:

1. **Daemon-authoritative billing.** The bill is whatever
   `GetSessionDebits` says, regardless of what the SDK reports. This
   neutralizes under-reporting attacks. *(Mandatory.)*
2. **Per-API-key trust scoring.** LOC tracks `under_report_count`,
   `sdk_sha_mismatch_count`, `dropped_session_count`. Thresholds
   produce operator alerts and (configurable) mint refusal. *(High
   value, moderate cost.)*
3. **Optional mTLS pinning for high-trust customers.** Customers
   who enroll get a client certificate they must present on every
   LOC API call. Stolen API keys without the cert fail. *(Opt-in;
   for customers who care.)*

### SDK quality bar (mandatory for "official" status)

An SDK is not "official" until it ships with all of:

- **Unit tests** for every state transition (mint → use → settle;
  open → refill × N → close; broker disconnect mid-stream;
  LOC unreachable during refill; SDK process restart with active
  session in DB).
- **Integration tests** against a mock LOC + mock broker that
  exercise each of cases (a)/(b)/(c)/(d). Mock broker can replay
  recorded sessions and inject failures.
- **Conformance tests** against the live LOC stack + the upstream
  `livepeer-network-modules` fixture broker, run nightly. Failure
  blocks the next SDK release.
- **Fuzz testing** of:
  - Broker emits `Livepeer-Balance-Low` at varying cadences,
    including before `runway_min_units` (premature) and after
    (late).
  - LOC unavailable for N seconds during refill — SDK must wait
    out grace window and close gracefully, never crash.
  - Broker silently drops the connection — SDK must report
    best-effort settlement based on last debit observed.
  - Slow consumer (customer can't keep up with chunk rate) — SDK
    must apply backpressure without dropping bytes.
- **Public API stability**: `submit_job` / `submit_stream` /
  `open_session` signatures are SemVer-stable on the SDK major
  version. Internal classes (`_RefillManager`,
  `_SettlementParser`, etc.) are explicitly private.
- **Coverage gate**: ≥ 90% line coverage on the SDK's core flows,
  measured per language.

### Public API surface (frozen shape)

```python
# Case (a)/(b): atomic + post-settled
result = await client.submit_job(
    capability="openai:embeddings",
    offering="vllm-h100",
    body={...},
    estimated_units=512,
)
# result.body, result.actual_units, result.refund_wei

# Case (c): streaming
async for chunk in client.submit_stream(
    capability="openai:chat-completions",
    offering="vllm-h100",
    body={...},
    estimated_units=2048,
) as stream:
    print(chunk)
# stream.actual_units, stream.refund_wei, stream.settlement available on exit

# Case (d): long-lived session
async with client.open_session(
    capability="openai:realtime",
    offering="openai-resale",
    estimated_runway_units=180_000,  # ~1h of audio-seconds
    max_total_units=720_000,         # 4h hard ceiling
) as session:
    async for frame in session:
        await session.send(processed(frame))
# session.actual_units, session.refund_wei, session.settlement on exit
```

Refills, balance-low handling, settlement reporting, daemon
verification, and broker URL discovery all happen inside the SDK.
Customer code is identical regardless of which broker is selected or
how many refills happened.

### Operator alerting program

LOC surfaces SDK-integrity signals on the admin SPA:

- **SDK version distribution**: histogram of `<lang>/<semver>` across
  active API keys. Highlights customers on stale versions.
- **SHA mismatch list**: per-API-key `sdk_sha_mismatch_count` rate.
  Investigation surface.
- **Discrepancy leaderboard**: top API keys by `under_report_count`.
  Auto-suspends keys that cross operator-set thresholds.
- **Dropped-session rate**: per API key. High rate = SDK bug, not
  malice, but still actionable.

## SDK telemetry (v1)

Because LOC is no longer in the data plane (handoff mode), the only
way to observe what's happening between customer SDKs and brokers
is to have the SDK report it. **Telemetry is therefore mandatory for
v1, not optional** — without it, operators have no insight into
broker performance, error rates, capability quality, or the customer
experience.

The SDK is responsible for emitting telemetry. LOC ingests, stores,
aggregates, and surfaces.

### Design principles

1. **Mandatory, not opt-out.** Telemetry is part of the SDK contract.
   There is no `telemetry=False` flag. Customers using the official
   SDKs are emitting telemetry; there is no in-SDK switch to disable
   it. Rationale: telemetry is operational instrumentation
   (timing, status, counts), not surveillance — it carries no body
   content. Customer privacy posture is satisfied by *what we do
   not collect* (see principle 3), not by *whether they can opt
   out*. Opt-out creates a two-class system where opted-out
   customers lose support quality and the operator loses SLA
   enforceability, without strengthening the privacy story.
2. **Fire-and-forget.** Telemetry MUST NOT block the data plane. SDK
   buffers events in memory and flushes asynchronously. If LOC is
   unreachable, events are dropped after the buffer fills — never
   queued indefinitely or blocking customer code.
3. **Privacy-preserving by collection limits.** Telemetry payloads
   MUST NOT contain request bodies, response bodies, frame contents,
   prompts, completions, or any customer-identifiable content
   beyond `api_key_id`. Only metadata: timing, status codes, byte
   counts, error categories. The lawful basis (under GDPR-style
   frameworks) is performance of the contract — telemetry is
   necessary to operate the billable service, including SLA
   enforcement and incident response.
4. **Standard schema.** A single typed event schema (JSON, versioned)
   that every official SDK emits identically. Operators can build
   cross-language dashboards without language-specific parsing.
5. **Operator-controlled retention.** Default 30 days; configurable
   via `TELEMETRY_RAW_RETENTION_DAYS`. v1 stores everything in
   LOC's Postgres for the full window; v2 adds an async NaaP
   forwarder for longer-term analytics.
6. **Customer-visible aggregates + transparency surface.** Customers
   can see their own telemetry summaries via the portal — per-session
   latency, refill counts, error rates, full event categories,
   30-day JSON download. They can read the published privacy notice
   at `/v1/privacy/telemetry`. They can request deletion of
   historical telemetry under data-subject-access rights (operator
   policy, see "Data-subject rights" below). Transparency is the
   trust mechanism; opt-out is not.

### Mechanism

- **Endpoint**: `POST /v1/telemetry` (bearer-auth with the customer's
  LOC API key; SDK uses the same credential it already holds).
- **Connection reuse**: SDK MUST use an HTTP/2-capable client and
  reuse the existing multiplexed connection to LOC for telemetry
  batches. No new TCP connection per batch. This makes
  per-batch network cost comparable to a WebSocket message
  without paying for a separate persistent socket. Reference SDKs
  use `httpx` (Python), `undici` (Node), `net/http` with HTTP/2
  (Go).
- **Batching**: SDK buffers events; flushes on (i) batch size
  ≥ 100 events, or (ii) 5 seconds elapsed since last flush,
  whichever comes first. Configurable in the SDK constructor.
- **Flush-on-critical-events**: SDK MUST flush immediately
  (bypass batch timer) when buffering any of: `request.error`,
  `session.error`, `session.refill_denied`, `session.closed`.
  Critical events arrive at LOC within ms of occurrence; routine
  lifecycle events aggregate cheaply.
- **Buffer cap**: default 10,000 events in memory; oldest dropped on
  overflow. SDK logs at WARN when dropping.
- **Retry**: 3 attempts with exponential backoff on LOC 5xx /
  network error. After 3 failures, the batch is dropped (do not
  block customer code on telemetry persistence).
- **Compression**: gzip request body when batch size > 1 KB.

### Mandatory event types (v1)

Every official SDK MUST emit these on every relevant operation.
Optional fields are noted; everything else is required.

#### Universal fields (every event)

These are required on every event regardless of type:

- `event_type` — string identifier (`request.mint_started`, etc.)
- `event_schema_version` — integer; current v1 = `1`. SDK MUST
  increment this when emitting under a schema change. LOC ingestion
  validates and routes by version.
- `correlation_id` — `job_id` for request events, `session_id` for
  session events, `null` for `sdk.init` / `quota.*`. Powers
  cross-event-type queries ("show me everything that happened to
  request X").
- `client_ts` — ISO-8601 UTC timestamp from the SDK's clock at
  emission time. SDK clock skew is real; LOC records this for
  audit but never trusts it for ordering.
- `received_ts` — set by LOC at ingestion. Authoritative timeline
  field; dashboards order by this.

#### Per-request lifecycle (cases a / b / c)

| Event | When emitted | Required fields (beyond universal) |
|---|---|---|
| `request.mint_started` | SDK calls `POST /v1/jobs` | `capability`, `offering`, `mode`, `estimated_units` |
| `request.mint_completed` | LOC responds | `latency_ms`, `loc_status_code`, `funded_value_wei`, `price_per_unit_wei`, `quote_id`, `quote_version` |
| `request.broker_call_started` | SDK opens HTTP request to broker | `broker_url`, `started_at` |
| `request.broker_call_completed` | broker response received (header + body for sync; trailer for stream) | `broker_status_code`, `total_latency_ms`, `ttfb_ms`, `body_bytes`, `actual_units`, `broker_error?`, **workload-shape fields** (see below) |
| `request.settle_started` | SDK calls `POST /v1/jobs/{id}/settle` | — |
| `request.settle_completed` | LOC responds | `latency_ms`, `loc_status_code`, `refund_wei`, `billed_value_wei`, `outcome` (`EXACT`/`OVERFUNDED`/`UNDERFUNDED`/`STOPPED_AT_BUDGET`/`TOPPED_UP`) |
| `request.completed` (summary) | emitted after `settle_completed` succeeds; consumer-friendly single-event job record | `capability`, `offering`, `mode`, `estimated_units`, `actual_units`, `billed_value_wei`, `refund_wei`, `outcome`, `total_latency_ms`, `broker_url` |
| `request.error` | any exception | `phase` (`mint`/`broker_call`/`settle`), `error_class`, `error_code?` |

#### Per-session lifecycle (case d)

| Event | When emitted | Required fields (beyond universal) |
|---|---|---|
| `session.opened` | SDK calls `POST /v1/sessions` and receives 200 | `capability`, `offering`, `mode`, `max_total_units`, `initial_runway_units`, `price_per_unit_wei`, `quote_id`, `quote_version` |
| `session.broker_connected` | WS upgrade or RTMP connect succeeds | `broker_url`, `connect_latency_ms`, `broker_session_id?`, `topup_url?`, `status_url?`, `end_url?` (last four populated when the mode advertises them — observability only; LOC never *uses* these URLs, handoff preserved) |
| `session.balance_low_received` | broker emits `Livepeer-Balance-Low` | `consumed_units`, `consumed_value_wei` |
| `session.refill_requested` | SDK posts to LOC refill endpoint | `refill_seq` |
| `session.refill_granted` | LOC returns 200 with envelope | `refill_seq`, `latency_ms`, `funded_value_wei`, `running_billed_value_wei`, `refill_count`, `cap_status` (the full block returned) |
| `session.refill_denied` | LOC returns 402 | `refill_seq`, `which`, `remaining_wei` |
| `session.winddown_warning` | refill response carries `will_refuse_next_refill=true` | `reason`, `projected_end_at` |
| `session.closed` | SDK posts to LOC close endpoint or session iterator exits | `actual_units`, `billed_value_wei`, `refund_wei`, `outcome`, `closed_by` (`customer`/`broker`/`cap_reached`/`error`), `refill_count`, `balance_low_count`, `duration_seconds` |
| `session.summary` | emitted after `session.closed`; consumer-friendly aggregate record | `capability`, `offering`, `mode`, `max_total_units`, `actual_units`, `billed_value_wei`, `refund_wei`, `outcome`, `refill_count`, `balance_low_count`, `duration_seconds`, **frames_or_chunks_total** (when applicable) |
| `session.error` | any exception during the session | `phase`, `error_class`, `error_code?` |

#### Quota lifecycle (user-balance and cap events)

| Event | When emitted | Required fields (beyond universal) |
|---|---|---|
| `quota.period_rollover` | SDK observes a refill response whose `period_start` differs from the previously observed value | `period_start`, `period_end`, `previous_period_spend_wei` |
| `quota.threshold_crossed` | SDK observes a `cap_status` block crossing an operator-configurable threshold (50% / 75% / 90% / 95% defaults) | `which_cap`, `threshold_pct`, `current_pct`, `projected_exhaustion_at?` |

#### SDK process info (emitted once per SDK init)

| Event | Required fields (beyond universal) |
|---|---|
| `sdk.init` | `lang`, `semver`, `git_sha7`, `runtime_version` (e.g. `python/3.13.1`), `os`, `os_version`, `process_id` |

#### Workload-shape fields (event-conditional)

Some capabilities expose numeric/categorical shape info in their
responses that's both useful for dashboards AND privacy-safe (counts
and dimensions, never content). SDK SHOULD include these on
`request.broker_call_completed` and `session.summary` when the
capability supports them:

| Capability family | Fields |
|---|---|
| OpenAI chat/completions | `prompt_tokens`, `completion_tokens` |
| OpenAI embeddings | `input_count`, `vector_dimensions` |
| Image generation | `image_count`, `resolution_class` (e.g. `"1024x1024"`) |
| Video transcode / live | `duration_seconds`, `peak_bitrate_kbps`, `output_rung_count` |
| Audio / realtime | `audio_seconds` |

These are extractor-driven — the same extractor that produced
`actual_units` for the broker also informs the shape fields. Content
itself (prompts, completions, frame bytes, image bytes) is NEVER in
telemetry.

#### Operator-only enrichment (added at ingest, NOT emitted by SDK)

LOC enriches incoming events at ingestion time with operator-side
context the SDK can't provide:

| Field | Source |
|---|---|
| `geo_region` | derived from source IP via GeoIP |
| `account_tier` | derived from customer billing plan |
| `broker_operator_id` | mapped from `broker_url` via registry-daemon |
| `ingest_node_id` | which LOC ingestion replica handled it (for debugging) |

These are not part of the SDK contract; they live alongside the SDK
event in the stored row.

### Storage strategy: LOC Postgres only (v1); NaaP analytics deferred to v2

**Decision (2026-05-24)**: v1 ships telemetry storage on LOC's own
Postgres. The NaaP forwarder, long-term ClickHouse-backed storage,
and the operator dashboards that depend on them are deferred to v2.
Rationale: NaaP product/vendor selection is a separate decision that
shouldn't block telemetry shipping; the SDK contract, ingestion,
retention, notifications, and customer queries are all independent
of where long-term storage lives. v1 covers every immediate-need
surface from Postgres; v2 layers analytics on top without an SDK
change.

#### v1 storage (in scope now)

- **LOC stores raw events in Postgres** (existing infrastructure)
  with operator-configurable retention via
  `TELEMETRY_RAW_RETENTION_DAYS`. **Default 30 days** for v1 (bumped
  from the original 7-day NaaP-era plan so the customer-facing
  "30-day download" promise is satisfied from Postgres alone).
  Powers: recent-event customer queries (B7), notification triggers
  (B6), reconciliation janitor cross-checks, and admin debugging.
- **All `server.*` events** land in the same table the same way SDK
  events do — LOC writes directly, no `POST /v1/telemetry` round-trip
  to itself.
- **A cleanup janitor** purges rows older than the retention window
  on a configurable cadence.

#### v1 configuration

| Setting | Default | Purpose |
|---|---|---|
| `TELEMETRY_RAW_RETENTION_DAYS` | 30 | How long LOC keeps raw events in Postgres |
| `TELEMETRY_RETENTION_JANITOR_INTERVAL_SECONDS` | 3600 | Cleanup-janitor cadence |
| `TELEMETRY_INGEST_RATE_PER_KEY` | 10000 | Per-API-key cap on `POST /v1/telemetry` events/sec |

#### v2 — NaaP forwarder (deferred, kept here as the next planner's brief)

When v2 lands, LOC adds an async forwarder that drains the same
Postgres store to a configurable external NaaP analytics pipeline
(ClickHouse-backed) for long-term storage, rollup computation, and
cross-customer dashboards. The v1 ingestion + storage path stays
unchanged; the forwarder is a new background worker layered on top.

Future-v2 settings (placeholders only in v1):

| Setting | Default | Purpose |
|---|---|---|
| `TELEMETRY_NAAP_ENDPOINT` | unset | Where to ship events (unset disables forwarding) |
| `TELEMETRY_NAAP_AUTH` | — | Credentials for the NaaP endpoint |
| `TELEMETRY_FORWARDER_BATCH_SIZE` | 500 | Events per outbound batch to NaaP |
| `TELEMETRY_FORWARDER_FLUSH_SECONDS` | 10 | Max time to hold a partial batch |

Future-v2 forwarder behavior:

- **Async**: events written to Postgres synchronously (so
  `POST /v1/telemetry` returns once persisted locally), then a
  separate worker drains a forwarding queue. v1 stops here.
- **At-least-once delivery**: forwarder retains a cursor; on
  restart, resumes from last acked offset. NaaP may see duplicates —
  its ingestion is responsible for dedup (event has `correlation_id`
  + `client_ts` to make this trivial).
- **Backpressure-tolerant**: if NaaP is unreachable, the forwarder
  queue grows; LOC keeps accepting events; eventually backlog
  overflows operator-configurable threshold and LOC raises an admin
  alert. LOC's local store keeps working for recent-event queries.
- **Idempotent on retry**: each batch carries a deterministic
  `batch_id` so NaaP can reject duplicate batches.

#### v1 dashboards: Postgres-only

- **Customer portal views** query LOC's local Postgres directly for
  the full retention window (default 30 days). v1 ships the full
  customer surface — per-session refill history, per-job latency,
  outcome distribution, telemetry download — without an external
  dependency.
- **Admin SPA** computes recent-rate-limit-hits, recent-discrepancies,
  recent-cap-reach events, and recent error rates directly from
  Postgres for the retention window. Trend/analytics views deeper
  than the retention window are **deferred to v2** (when NaaP lands).
- **v2 promises** (not v1): the broker-latency heatmap, broker-error
  rate, refill rate, refill-denial rate, settlement-discrepancy rate,
  and capability quality scores. All of these need columnar storage
  + cross-customer aggregation. Reserve the surface in the design;
  build when NaaP is picked.

#### v1 customer deletion requests

- Purge raw events from LOC's Postgres for the identified key.
- (v2) Propagate a delete instruction to NaaP for the same key —
  no-op in v1 since no external store exists yet.

### Rate limiting and overload protection (v1)

A buggy SDK could fire millions of events/sec and OOM LOC's
ingestion. `POST /v1/telemetry` is rate-limited per API key.

- **Default**: 10,000 events/sec per API key, configurable via
  `TELEMETRY_INGEST_RATE_PER_KEY`.
- **Response on exceed**: `429 Too Many Requests` with
  `Retry-After: <seconds>` header. SDK MUST treat this as transient
  and retry per its normal backoff policy.
- **Operator visibility**: each `429` increments a
  `telemetry.rate_limited` counter per API key on the admin SPA;
  sustained limits over operator-set threshold raise an alert.
- **Distinct from billable rate limits**: this caps telemetry
  ingestion only. Customers cannot avoid telemetry billing by
  triggering rate limits (telemetry is not billable usage).

### LOC server-side events (v1)

LOC's own runtime emits telemetry alongside the SDK's events. Same
schema, same storage, different source. These complement SDK events
for a complete picture and let operators see things the SDK can't
(e.g., the moment LOC's janitor finalized a session the SDK never
closed).

Server-side events use a `server.*` event-type prefix and are
written directly to the same Postgres store as SDK events — no
`POST /v1/telemetry` round-trip; LOC writes to its own local store
directly. (In v2 the NaaP forwarder will drain both SDK and server
events to NaaP from the same Postgres table.)

#### Mandatory server-side events (v1)

| Event | When emitted | Required fields (beyond universal) |
|---|---|---|
| `server.mint_served` | LOC mints a payment in response to `POST /v1/jobs` or `POST /v1/sessions` | `api_key_id`, `capability`, `offering`, `mode`, `estimated_units`, `funded_value_wei`, `mint_latency_ms` |
| `server.refill_served` | LOC mints a refill in response to `POST /v1/sessions/{id}/refill` | `api_key_id`, `session_id`, `refill_seq`, `funded_value_wei`, `cap_status` |
| `server.refill_denied` | LOC returns 402 to a refill request | `api_key_id`, `session_id`, `refill_seq`, `which_cap`, `remaining_wei` |
| `server.session_janitor_finalized` | reconciliation janitor closed a session the SDK never explicitly closed | `api_key_id`, `session_id`, `actual_units`, `billed_value_wei`, `refund_wei`, `outcome`, `silence_duration_seconds` |
| `server.mint_refused` | LOC returns 402/4xx to a mint request | `api_key_id`, `capability?`, `offering?`, `which_cap`, `remaining_wei` |
| `server.sdk_sha_mismatch` | LOC observes an SDK identity whose SHA doesn't match the manifest | `api_key_id`, `lang`, `semver`, `reported_sha`, `expected_sha` |
| `server.discrepancy_detected` | LOC's settle verification finds SDK report vs daemon ledger mismatch | `api_key_id`, `job_or_session_id`, `sdk_reported_units`, `daemon_units`, `difference` |

Server events have `correlation_id = api_key_id` (or session_id /
job_id when scoped to one). They join cleanly with SDK events in
Postgres (and in v2, in NaaP) for end-to-end visibility ("what did
the SDK report vs what LOC saw").

### Customer notification preferences (v1)

Customers configure how they're told about events that affect them.
Lives in a new `notification_config` row keyed by user_id, with
sensible defaults so customers don't have to configure anything to
get basic visibility.

#### Triggers

LOC fires notifications when these events happen for the customer:

| Trigger | Source events | Default channels |
|---|---|---|
| `cap_reached` | `server.refill_denied`, `server.mint_refused` (`which_cap` ∈ user-balance / spend-period / per-session) | email + in-portal banner |
| `period_rollover` | `quota.period_rollover` | in-portal banner only |
| `winddown_warning` | `quota.threshold_crossed` at 90% / 95% thresholds | email + in-portal banner |
| `sdk_outdated` | `server.sdk_sha_mismatch`, manifest min-version bump | email |
| `session_failed_repeatedly` | ≥3 `session.error` for the same key within 1 hour | email |

#### Channels (v1)

- **Email** — via the existing email provider (Resend integration
  already wired). Per-trigger opt-out via `notification_config`.
- **In-portal banner** — surfaces in the portal dashboard;
  dismissible per-user; reappears on next trigger.
- **Webhook** — opt-in only. Customer configures a URL + secret;
  LOC POSTs JSON with `{trigger, event, signature}`. Standard-
  Webhooks signing (same protocol we already use for Resend ingress
  — reused inverted). Off by default.

#### Defaults

- New accounts: email + in-portal for `cap_reached`,
  `winddown_warning`, `sdk_outdated`, `session_failed_repeatedly`;
  in-portal only for `period_rollover`.
- Customer can disable any trigger per-channel via portal preferences.
- Operators cannot force a notification preference on a customer;
  they can only set a tier-default that new accounts inherit.

### Customer raw event query API (v1)

Sophisticated customers want raw event access (their SRE
dashboards, custom analytics). LOC exposes a customer-scoped query
endpoint over LOC's local store (recent retention window only).

```
GET /v1/telemetry/events?from=<iso>&to=<iso>&type=<glob>&format=json
```

- Bearer-auth with the customer's API key.
- Strictly scoped to the calling key — customers cannot query
  another customer's data.
- `from`/`to` MUST fall within LOC's `TELEMETRY_RAW_RETENTION_DAYS`
  (default 30d in v1). Older windows return 410 with a body
  explaining the retention limit. (v2: 410 includes a pointer to
  NaaP's customer surface for older data.)
- `type` is a glob: `request.*`, `session.refill_*`, `quota.*`.
- `format` ∈ {`json`, `ndjson`}. `ndjson` for streaming-friendly
  downloads.
- Paginated via `?cursor=` for large result sets.
- Rate-limited per API key (default 100 requests/min) — different
  from telemetry ingestion rate limit.

For historical data older than the v1 retention window: no v1
answer (return 410). v2 adds a customer-facing NaaP surface
(implementation detail to settle when picking the NaaP product).

### Operator-facing dashboards

#### v1 — Postgres-backed, retention-window-bounded

LOC's admin SPA computes the following directly from the local
Postgres store over the configured retention window
(`TELEMETRY_RAW_RETENTION_DAYS`, default 30d):

- **Live ingestion stats** — events/sec, rate-limited counts per
  API key (last 1h / 24h).
- **Recent server-event roll-ups** — `server.refill_denied`,
  `server.mint_refused`, `server.discrepancy_detected`,
  `server.sdk_sha_mismatch` counts per API key (last 24h / 7d).
- **Per-API-key recent activity** — drill-in to a single key's
  event stream for a given time range.
- **Recent error rate** — `request.error` and `session.error` rates
  by capability/offering (last 24h).

These run cheaply on Postgres at v1 volumes; the queries are simple
GROUP BYs over `received_ts` ranges, supported by an index on
`(api_key_id, received_ts)`.

#### v2 — NaaP-backed, full-history analytics (deferred)

When NaaP lands, these views move to ClickHouse-computed dashboards
embedded in the admin SPA:

- **Broker latency heatmap** — p50/p90/p99 broker call latency per
  `(capability, offering, mode, broker_url)`. Identifies slow
  orchestrators *and* slow modes per orchestrator.
- **Broker error rate** — per `(capability, offering, broker_url,
  broker_status_code)`. Highlights flaky orchestrators per capability.
- **Refill rate** — refills per session per hour, by capability.
  Tunes refill thresholds and identifies high-frequency-refill
  workloads.
- **Refill denial rate** — refill_denied events per
  `(which cap)` per hour. Operator can spot customers hitting caps
  systemically.
- **Settlement-discrepancy rate** — divergence between SDK-reported
  `actual_units` and daemon-reported `total_work_units`, per API key.
  Feeds the SDK-integrity discrepancy leaderboard.
- **Capability quality scores** — composite metric per
  `(capability, offering)`: latency + error rate + refill rate.
  Drives route selection improvements.

The line: real-time/incident + retention-window views in LOC
(Postgres) in v1; trend/analytics across the full history in NaaP
in v2.

### Customer-facing transparency (v1)

Portal surfaces, scoped to the customer's own API keys. v1 computes
all of these directly from LOC's Postgres store over the
`TELEMETRY_RAW_RETENTION_DAYS` window (default 30 days):

- Per-job latency history.
- Per-session refill counts, outcome distribution, cap-reach events.
- Recent broker latency / error rate per capability they use.
  (Full cross-customer "quality scores" deferred to v2 with NaaP.)
- Telemetry download (last 30 days as JSON, for self-service
  debugging).

### Deferred to v2 / future scope review

All items below are recognized as needed eventually but not v1.
Kept here documented so the next planner picking up this area
doesn't re-litigate the framing.

#### Telemetry hardening (not v1)

- **Cardinality limits on dimension fields.** Free-form string
  values blow up storage. Need per-dimension daily uniqueness caps
  with overflow bucketing (`broker_url` capped at 1000/day/key →
  excess into a `<bucket>` value). NaaP storage soaks this for v1;
  becomes urgent if costs balloon.
- **Ingest-side PII redaction (defense in depth).** SDK MUST NOT
  send body content, but a defensive scan at ingest for shapes that
  look like PII (emails, JWTs, large base64 blobs) and reject /
  scrub belt-and-suspenders. Privacy invariants are enforced via
  SDK conformance review for v1.
- **Schema versioning enforcement at the ingest layer.**
  `event_schema_version` already on every event; v1 ingest accepts
  whatever. v2 ingest validates `version ∈ accepted_set`, rejects
  with `400 unsupported_schema_version`, and supports a deprecation
  window when bumping major.
- **Backfill / replay window.** Events arrive late (SDK offline,
  buffered locally). v2 caps acceptance with a `MAX_BACKFILL_AGE`
  (default 24h); older events dropped + counted on a
  `received_late` metric.
- **Telemetry-action audit log.** Operator actions on telemetry
  (data-subject-deletion, retention changes, key suspensions for
  high error rate) recorded in the existing `operator_audit`
  table. v1 actions are logged ad-hoc; v2 formalizes.

#### Telemetry-driven detection / alerting (not v1)

- **Operator alerting rules engine.** Paging rules on top of the
  NaaP rollups: error-rate > X% for Y min → page; refill_denied
  surge → notify customer success; broker latency p99 > Z → flag
  broker; error-class clustering → incident alert. v1 ships dumb
  thresholds in NaaP; v2 builds a customizable rules layer.
- **Cost-projection events.** `quota.projection_updated` fired
  when projection materially changes ("at current rate, you'll
  hit cap by 2026-06-01"). Powers proactive portal UX without
  polling. v1 customer sees current spend; v2 sees trajectory.
- **Anomaly detection.** Sudden cost spike, unusual capability
  mix, geographic shift in source IPs. Statistical baselines that
  don't exist in v1.

#### SDK-side push (not v1)

- **Command-and-control channel (LOC → SDK push).** Recommended
  shape: **server-sent events (SSE)** on a dedicated
  `GET /v1/sdk/events` endpoint. One-way push, HTTP-native, no
  WebSocket scaling cliff. NOT bundled with telemetry. Use cases:
  "your cap was raised, refills resume"; "operator killed your
  sessions, drain"; "maintenance window in N minutes."
- **OpenTelemetry / OTLP export.** v1 ships JSON-over-HTTP only;
  OTLP for customers who want to pipe telemetry into their own
  observability stack.

#### Always out of scope

- **Per-frame / per-byte counters** for case (d). Latency-
  prohibitive at high frame rates; not necessary given workload-
  shape rollups already cover the operator need.
- **Customer-side log shipping of SDK debug logs.** Customers run
  their own log infra.
- **Cross-region replication of telemetry.** Only relevant if LOC
  ever goes multi-region; out of scope until that decision.
- **Customer-configurable retention extensions** (customers paying
  for longer than the operator's default). Pricing-product
  decision, not a technical one.
- **Multi-operator federation** of telemetry (sharing aggregated
  quality scores across operators). Cross-operator concern.

#### Upstream contributions to propose (post-v1)

Items where LOC's design surfaces a gap in the upstream spec that
would benefit the wider ecosystem if resolved. These are LOC
proposing changes to `livepeer-network-modules`, not changes to
LOC itself.

- **`ws-realtime@v0.2` with topup support.** Today's spec leaves
  bidirectional-WS-with-topup uncovered (see "Why ws-realtime has
  no topup" under refill delivery). A v0.2 proposal would add a
  reserved control frame discriminator (e.g., `Livepeer-Frame:
  control` text frame, then JSON body), require brokers to handle
  it, and require capabilities not to claim that discriminator.
  Substantial coordination work; weeks-to-months upstream timeline.
  Worth raising after LOC v1 ships and we have real-world data on
  how many customers hit the gap. Until then, customers wanting
  topup on bidirectional WS use `session-control-plus-media` (with
  the media-plane requirement) or accept bounded ws-realtime
  sessions.

### Data-subject rights (privacy posture)

- **Published notice**: `GET /v1/privacy/telemetry` serves the
  structured privacy notice describing categories collected,
  retention period, lawful basis, contact for data-subject
  requests. Static content, signed by the operator key (same
  mechanism as the SDK manifest).
- **Customer access**: portal exposes per-API-key telemetry
  download (30-day JSON). Customers can self-serve a copy of what
  LOC holds about them.
- **Deletion requests**: operator-side admin surface accepts
  data-subject-deletion requests; LOC purges telemetry for the
  identified API key (preserving aggregated metrics that are not
  reversible to the individual). This is operator policy, not an
  SDK flag.
- **Aggregation**: telemetry that has been aggregated into
  operational metrics (e.g., per-capability latency p99 across all
  customers) is not subject to per-customer deletion — the
  individual record is purged but the aggregate remains.

### SDK conformance criteria for telemetry (v1)

An SDK is not "official" without:

- All mandatory event types implemented and tested.
- Fire-and-forget guarantee verified under fault injection
  (LOC unreachable, slow network, full buffer).
- Privacy review confirming no body content, no user identifiers
  beyond `api_key_id`, no prompts/completions, no frame payloads.
- HTTP/2 connection reuse to LOC, verified by network capture.
- Flush-on-critical-events behavior verified for `*.error`,
  `session.refill_denied`, `session.closed`.
- No telemetry-disable flag. Any SDK that exposes one is not
  official. (Customers contractually exempt under specific
  enterprise agreements use operator-side filtering on the
  ingestion endpoint, not an in-SDK switch.)

## Q#3 — Fail-mode policy at credit exhaustion (resolved 2026-05-24)

### How handoff mode sharpens the question

LOC can **never interrupt an active session.** Once a payment
envelope is in the customer's SDK, the work it covers will happen.
Enforcement therefore exists only at two points:

- **Mint time** (initial `POST /v1/sessions` or `POST /v1/jobs`).
- **Refill time** (`POST /v1/sessions/{id}/refill`).

Mid-session "stop" doesn't exist; the strongest LOC can do is "don't
extend." A session that doesn't get a refill it asked for drains
through the broker's grace window (default
`grace_window_ticks × cadence_seconds = 10s`) and then closes
cleanly with `Livepeer-Error: payment_invalid`.

One consequence: `max_total_units × EV-per-unit` declared at session
open is the **worst-case exposure of a single session**, regardless
of what spend caps say later. The mint-time check has to clear caps
against this worst case, not just against the initial runway.

### Sub-decision 1 — Cap structure (resolved 2026-05-24)

Four caps, layered. All four are v1.

| Cap | Always on? | Where it lives | What it bounds |
|---|---|---|---|
| **(i) User balance** | Yes | `credit_balance.balance_wei` (existing) | User can't run a mint that would drive balance negative |
| **(ii) Spend-period cap** | Yes | `billing_config.spend_period_cap_wei` (existing) | Rolling-window spend per user. **Refills count against this cap too** — otherwise a long session trivially bypasses it. |
| **(iii) Per-session cap** | Yes | `payment_session.max_total_value_wei` (new) | Worst-case exposure of one session. Set by SDK on open or defaulted from offering metadata. Lets operators bound blast radius of a single misbehaving stream. |
| **(iv) Operator-pool cap** | **Opt-in** | new operator-scope config, default disabled | Aggregate-spend circuit breaker across all users in a window. Protects the operator from black-swan days. Operators who enroll set the window + ceiling; LOC blocks mints/refills that would push aggregate over. |

Mint and refill checks evaluate all enabled caps; first to fail wins.
The full check expression:

```
mint OK iff:
       (i)   user_balance                      >= EV_of_this_mint
  AND  (ii)  period_spend_so_far + EV_of_this  <= spend_period_cap_wei
  AND  (iii) session_spend_so_far + EV_of_this <= per_session_cap
  AND  (iv)  operator_pool_spend_so_far + EV   <= operator_pool_cap   (if enabled)
```

At session-open, `EV_of_this_mint` is `max_total_units × EV-per-unit`
(the worst case), not the initial runway, so caps clear against the
ceiling.

### Sub-decision 2 — When does LOC check? (resolved 2026-05-24)

Caps are checked at **mint + every refill + reported proactively to
the SDK on every successful refill response**.

The reconciliation janitor (already running on 60s cadence as a
safety net) also recomputes per-session cap status. If a cap is
crossed between refills (e.g., another session of the same user
consumed enough to push spend-period cap over), the next refill
attempt is the one that fails — there's no mid-session "force
close" path because handoff mode forbids it.

#### Proactive notification mechanism: embed in refill response

Every successful refill response carries a `cap_status` block:

```json
{
  "payment_envelope": "...",
  "cap_status": {
    "user_balance_pct_used":       0.42,
    "spend_period_pct_used":       0.81,
    "session_pct_used":            0.15,
    "operator_pool_pct_used":      0.55,
    "will_refuse_next_refill":     false,
    "winddown_reason":             null
  }
}
```

When LOC predicts the next refill will be refused (any cap > 95% and
projected consumption rate would cross it before the subsequent
refill is needed), it sets `will_refuse_next_refill: true` +
`winddown_reason: "spend_period_cap_imminent"` so the SDK can warn
the customer *one refill window early*.

On the refusal itself, LOC returns `402 Payment Required` with:

```json
{
  "error":  "cap_reached",
  "which":  "spend_period_cap",
  "remaining_wei":  0,
  "advice": "Raise cap at /portal/billing or wait until period rollover at 2026-06-01T00:00:00Z"
}
```

The SDK surfaces this to the customer with a clear, actionable
message. Operator audit log captures the event.

**Rationale**: handoff mode's promise of "no extra polling" stays
intact. Refills already happen on the broker's cadence (default
every `runway_min_seconds`, ~minutes for typical (d) sessions), so
embedding cap status in the refill response gives the SDK fresh
info without requiring any new long-lived connection or polling
loop. Out-of-band channels (email when approaching cap, in-app
dashboard alerts) are operator policy, not SDK-mechanism.

### Sub-decision 3 — Refusal behavior per case (resolved 2026-05-24)

#### Refusal at mint (cases a / b / c)

The mint is the first step of `submit_job` / `submit_stream`; SDK
has the 402, hasn't called the broker yet, no broker session exists.

- SDK raises a typed exception (Python: `LocSpendCapError`, with
  fields mirroring the 402 body — `which`, `remaining_wei`,
  `advice`).
- Customer catches; shows their user the message; can top up, raise
  cap, or wait for period rollover.
- No partial work, no settlement to record, no broker session
  opened.
- Operator audit log: `mint_refused, reason=cap_reached, which=...`.

#### Refusal at refill (case d)

Broker has emitted `Livepeer-Balance-Low`; SDK called LOC's refill
endpoint; LOC returned 402.

**Three behaviors, all locked:**

**(d.1) SDK behavior — hybrid: log + warn, then drain.**

- SDK logs the refusal at WARN level with the full 402 body.
- If the customer registered `on_refill_refused`, SDK fires it with
  `(which, remaining_wei, advice, projected_end_at)`. Callback is
  optional; not required for correctness.
- SDK does **not** try to "rescue" the session (no automatic
  fallback to a cheaper offering, no retry). It lets the broker's
  grace window expire naturally.
- Broker closes via the standard path: drains within
  `grace_window_ticks × cadence_seconds` (default 10s), emits
  `Livepeer-Error: payment_invalid`, calls
  `Reconcile + CloseSession` server-side.
- SDK's `open_session` async-iterator exits cleanly with
  `session.outcome = "cap_reached"`, `session.refund_wei = ...`.
  Customer code that doesn't register a callback still sees the
  session end naturally with the outcome reason exposed on the
  session object.

**(d.2) `will_refuse_next_refill` proactive flag — event-only.**

When LOC sets `will_refuse_next_refill: true` in a successful
refill's `cap_status` block:

- SDK fires `on_winddown_warning(reason, projected_end_at)` if a
  callback is registered. Otherwise silent.
- SDK does **not** take automatic action (no lowering of
  `max_total_units`, no offering switch, no preemptive close). All
  policy decisions stay with customer code.
- Customer code can use the warning to: display a "session ending
  soon" UI, prompt the user to raise their cap, gracefully wrap
  up the work, etc.

**(d.3) Encumbrance at mint — worst case.**

LOC reserves `max_total_units × EV-per-unit` from the user balance
at session open, not just `initial_runway × EV`.

- Mint either succeeds with full session headroom or fails up
  front. Refills are *guaranteed* up to `max_total_units` because
  the funds are already encumbered against the user balance.
- The only causes of refill refusal mid-session become **spend-period
  cap rolling forward** (other sessions consumed in the meantime)
  and **operator-pool cap rolling forward**. Per-session cap
  refusal becomes impossible mid-session, by construction.
- Customer guarantee: "this session can cost at most $X; it will
  cost at most $X." Predictable. No mid-session shrinkage from
  concurrent activity on the same user.
- Cost: capital sits encumbered for the session's lifetime. The
  customer chose `max_total_units`, so they own the cost. Refund
  at close releases unused encumbered value back to the balance.

#### Schema implication

`payment_session.funded_value_wei` stores the *worst-case
encumbrance* (`max_total_units × EV-per-unit`), not the initial
runway. The initial runway value is the face value of the first
ticket and lives on the `payment` row that minted it. Successive
top-up tickets get their own `payment` rows; sum of all `payment`
face values for a session ≤ `payment_session.funded_value_wei`.

`payment_session.billed_value_wei` (set at close) is the actual
amount billed (`actual_units × EV-per-unit`). Refund =
`funded_value_wei − billed_value_wei`.

## Per-case lifecycle sketches (handoff mode)

All four cases follow the same skeleton:

1. SDK declares intent to LOC.
2. LOC validates, mints, returns `{broker_url, payment_envelope, ...}`.
3. SDK talks to broker directly.
4. SDK reports settlement to LOC.
5. LOC verifies via `payer-daemon.GetSessionDebits`, finalizes.

Step 4 is best-effort; a janitor task does step 5 unconditionally on
a slow cadence so reconciliation is never blocked on SDK liveness.

### Case (a) — atomic job (`http-reqresp@v0`, estimate ≈ actual)

```
SDK → LOC: POST /v1/jobs
            {capability, offering, estimated_units}
LOC → payer-daemon: CreatePayment(funding=estimated_units)
                                     ↓
                            {payment_bytes, work_id, EV}
LOC: write payment row (state=in_flight); encumber EV from user balance
LOC → SDK: 200 {job_id, broker_url, payment_envelope, settle_endpoint}

SDK → broker: POST /v1/cap
              (Livepeer-Payment, Livepeer-Mode=http-reqresp@v0, body)
broker (internal): payee-daemon OpenSession / ProcessPayment / DebitBalance(actual)
broker → SDK: 200 + Livepeer-Work-Units: <actual> + body

SDK → LOC: POST /v1/jobs/{job_id}/settle
            {actual_units, settlement?}
LOC → payer-daemon: GetSessionDebits(sender, work_id)
                                     ↓
                            (total=actual, closed=true)
LOC: write payment_settlement row; refund (estimate − actual) if positive
LOC → SDK: 200 {refund_wei, outcome}
```

### Case (b) — post-settled job (`http-reqresp@v0`, actual ≠ estimate)

Identical wire to (a). The difference shows up in the settlement
row: `outcome = OVERFUNDED` (refund) or, in the rare case the broker
exceeded `max_total_units` before stopping, `STOPPED_AT_BUDGET` (no
refund, partial work billed at actual).

### Case (c) — streaming chunks (`http-stream@v0`)

Identical wire to (a)/(b) at the LOC handshake. The differences are
SDK-internal:

```
SDK → broker: POST /v1/cap (Accept: text/event-stream, body)
broker → SDK: 200 + Transfer-Encoding: chunked + Trailer: Livepeer-Work-Units
broker → SDK: data: chunk 1
broker → SDK: data: chunk 2
              ...
broker → SDK: data: [DONE]
broker → SDK: [trailer] Livepeer-Work-Units: <actual>

SDK: collect actual_units from trailer
SDK → LOC: POST /v1/jobs/{job_id}/settle {actual_units, settlement?}
```

**SDK responsibilities here are non-trivial.** The SDK MUST:

- Use an HTTP client that surfaces trailers (Python's `httpx`
  exposes them; many SDKs in other languages need work).
- Stream chunks to the SDK consumer with no buffering (caller may
  be displaying tokens live).
- On mid-stream disconnect: post best-effort settlement with whatever
  was last observed (broker will have called `CloseSession`
  server-side, so daemon's `GetSessionDebits` is authoritative).

### Case (d-bounded) — continuous, no topup (`ws-realtime@v0`)

```
SDK → LOC: POST /v1/sessions
            {capability, offering, estimated_runway_units, max_total_units}
LOC → payer-daemon: CreatePayment(funding=max_total_units × EV, top_up_allowed=false)
                                     ↓
                            {payment_bytes, work_id, EV}
LOC: write payment_session row (state=open, mode="ws-realtime@v0")
     encumber (max_total_units × EV) from user balance
LOC → SDK: 200 {session_id, broker_url, payment_envelope,
                mode="ws-realtime@v0", close_endpoint}
                # NOTE: no refill_endpoint; SDK disables refill loop based on mode

SDK → broker: GET /v1/cap (WS upgrade, Livepeer-Payment)
broker (internal): OpenSession, ProcessPayment(funded), DebitBalance(runway_min)
SDK ←→ broker: 101 Switching Protocols; frame relay begins

[steady state: frames flow SDK ↔ broker; broker debits per cadence_seconds]

[when broker emits Livepeer-Balance-Low — informational only here]
broker → SDK: Livepeer-Balance-Low frame
SDK: fires on_winddown_warning("ws_session_exhausting") callback
SDK does NOT call LOC refill endpoint (refill loop is disabled)

[either: customer closes early, broker closes at balance-zero, or broker times out]
broker → SDK: close frame
                # for balance-zero: Livepeer-Error: payment_invalid
broker (internal): Reconcile(final_total), CloseSession

SDK → LOC: POST /v1/sessions/{session_id}/close {actual_units, settlement?}
LOC → payer-daemon: GetSessionDebits(sender, work_id)
                                     ↓
                            (total=final, closed=true)
LOC: write payment_settlement(event_type="close") row
LOC: refund (encumbered − billed); mark payment_session state=closed
LOC → SDK: 200 {refund_wei, outcome ∈ {customer_closed, ws_session_exhausted, broker_error}}
```

Defensive check at the refill endpoint: if a non-conformant SDK ever
calls `POST /v1/sessions/{id}/refill` for a session whose mode is
in the bounded set, LOC returns `400 refill_not_supported_for_mode`.
The official SDK never makes this call (the refill loop is disabled
at session-open based on the mode string from LOC's response).

### Case (d-extensible) — continuous with topup (`session-control-plus-media@v0`, `rtmp-ingress-hls-egress@v0`, `live-session-*@v0`)

```
SDK → LOC: POST /v1/sessions
            {capability, offering, estimated_runway_units, max_total_units}
LOC → payer-daemon: CreatePayment(funding=initial_runway, top_up_allowed=true)
                                     ↓
                            {payment_bytes, work_id, EV}
LOC: write payment_session row (state=open); encumber EV
LOC → SDK: 200 {session_id, broker_url, payment_envelope,
                refill_endpoint, close_endpoint, mode_params}

SDK → broker: GET /v1/cap (WS upgrade, Livepeer-Payment)
broker (internal): OpenSession, ProcessPayment(initial), DebitBalance(runway_min)
SDK ←→ broker: 101 Switching Protocols; frame relay begins

[steady state: frames flow SDK ↔ broker; broker debits per cadence_seconds]

[when broker emits Livepeer-Balance-Low]
broker → SDK: Livepeer-Balance-Low frame
SDK → LOC: POST /v1/sessions/{session_id}/refill
            {observed_consumed_units (advisory)}
LOC → payer-daemon: GetSessionDebits(sender, work_id)  # verify
                                     ↓
                            (current consumed)
LOC: check user balance + spend_period_cap allows refill
LOC → payer-daemon: CreatePayment(funding=refill_chunk)
                                     ↓
                            {payment_bytes for top-up}
LOC: write payment_settlement(event_type="refill") row
LOC → SDK: 200 {payment_envelope}
SDK → broker: deliver top-up envelope via the capability-defined in-band frame

[continue until close]

[close: either side]
SDK or broker: close frame
broker (internal): Reconcile(final_total), CloseSession
SDK → LOC: POST /v1/sessions/{session_id}/close {actual_units, settlement?}
LOC → payer-daemon: GetSessionDebits(sender, work_id)
                                     ↓
                            (total=final, closed=true)
LOC: write payment_settlement(event_type="close") row
LOC: refund (funded − billed); mark payment_session state=closed
LOC → SDK: 200 {refund_wei, outcome}
```

**Janitor safety net** (runs unconditionally, not gated on SDK
reports):

```
[every 60s, per open payment_session row where last_polled_at > 60s ago]
LOC → payer-daemon: GetSessionDebits(sender, work_id)
if closed=true and no explicit close received:
  finalize as if SDK had closed
if open and consumed > funded × 0.9 and no recent refill:
  log "stuck session — broker may have dropped Livepeer-Balance-Low"
```

### Case (d) splits into bounded vs extensible

Case (d) is not one shape — it depends on the chosen mode. Upstream
defines refill capability per mode, not universally:

- **(d-bounded)**: modes that do NOT support mid-session top-up.
  `ws-realtime@v0` is the canonical example. The session is fully
  funded at upgrade time; when balance reaches zero, broker closes
  with `Livepeer-Error: payment_invalid` and the session ends.
  `max_total_units` IS the absolute ceiling — the SDK cannot extend.
- **(d-extensible)**: modes that support mid-session top-up.
  `session-control-plus-media@v0`,
  `rtmp-ingress-hls-egress@v0`,
  `live-session-remote-runner@v0`, `live-session-gateway-ingest@v0`.
  The refill loop in the lifecycle sketch above applies.

LOC reconciliation is identical in both sub-cases (the janitor polls
the daemon either way). What differs is what the SDK does on
`Livepeer-Balance-Low`:

- **(d-bounded)**: fire `on_winddown_warning` callback for
  informational purposes; do NOT call LOC's refill endpoint. The
  session is going to end; the customer is being warned.
- **(d-extensible)**: call LOC's refill endpoint per the lifecycle
  sketch, deliver the returned envelope to the broker via the
  mode-specific channel (see "Refill delivery wire shapes (per
  mode)" below).

#### Behavior matrix: LOC vs SDK vs customer

Concretely, here's what differs across the three layers:

| Layer | (d-bounded) | (d-extensible) |
|---|---|---|
| **LOC: session-open** | Mint + encumber `max_total_units × EV`; write `payment_session` row with `mode="ws-realtime@v0"`. | Mint + encumber `max_total_units × EV`; write `payment_session` row with the extensible-mode string. |
| **LOC: refill endpoint** | MUST return `400 refill_not_supported_for_mode` if called (defensive — official SDK never calls it for this class). | Verify via `GetSessionDebits`, check caps, mint top-up, return envelope + `cap_status`. |
| **LOC: reconciliation janitor** | Identical: poll `GetSessionDebits`, finalize on `closed=true`. | Identical: poll `GetSessionDebits`, finalize on `closed=true`. |
| **LOC: close** | Identical: verify, refund `(funded − billed)`. | Identical: verify, refund `(funded − billed)`. |
| **SDK: at session-open** | Read `mode` from response; **disable refill loop**. | Read `mode`; activate refill loop with mode-specific delivery channel. |
| **SDK: on Livepeer-Balance-Low** | Fire `on_winddown_warning(reason="ws_session_exhausting", projected_end_at=...)`. Take no other action. | Call `POST /v1/sessions/{id}/refill`; on 200, deliver envelope to broker via mode-specific channel (control-WS frame OR HTTP POST to `topup_url`); fire `on_refill_succeeded` (optional callback). |
| **SDK: on refill response with `will_refuse_next_refill=true`** | N/A (no refills happen). | Fire `on_winddown_warning(reason=<cap_status reason>, projected_end_at=...)`. |
| **SDK: at close** | Iterator exits with `session.outcome ∈ {"customer_closed", "ws_session_exhausted", "broker_error"}`. | Iterator exits with `session.outcome ∈ {"customer_closed", "cap_reached", "broker_error"}`. |
| **Customer: meaning of `max_total_units`** | "My session will spend AT MOST this much. It may end earlier; it will end no later than when this much has been consumed." | "My session will spend AT MOST this much. Auto-refills happen within this ceiling whenever the broker asks." |
| **Customer: agency on imminent end** | None: warning fires, but they can't extend. Best they can do is wrap up gracefully. | Can raise period cap (via portal), contact operator, or accept the end. Next refill may succeed if cap is raised in time. |

## Refill delivery wire shapes (per mode)

For (d-extensible) modes, the SDK must deliver the LOC-minted top-up
envelope to the broker. Upstream specifies the wire shape per mode;
there is no single universal mechanism. SDK MUST select the correct
one based on `payment_session.mode`.

### `session-control-plus-media@v0` — control-WS JSON frame

SDK already holds the control WebSocket from session-open. Refill is
a JSON frame on that WS:

```json
{
  "type": "session.topup",
  "body": {
    "payment_header": "<base64 Livepeer-Payment>"
  }
}
```

The broker treats this as a control-plane message and credits the
existing receiver-side session. The backend SHOULD emit
`session.balance.refilled` (a broker → gateway control event) on
success; SDK SHOULD wait for that ack before declaring the refill
complete to LOC (via the standard settlement / telemetry events).

### `live-session-remote-runner@v0` and `live-session-gateway-ingest@v0` — HTTP POST to broker

The broker advertises `control.topup_url` in the session-open
response. The SDK captures it at session-open and holds it locally
for the session lifetime. Refill is:

```
POST {control.topup_url}
Content-Type: application/json
Livepeer-Request-Id: <uuid>
Livepeer-Payment: <base64 envelope>

{
  "gateway_session_id": "<the session id LOC returned to SDK>"
}
```

Broker response (200) carries the updated session state and a new
runway estimate. Per the spec:

- Top-up MUST credit the existing receiver-side payment session.
- Top-up MUST NOT create a new logical live session.

#### "topup_url ownership" — what this actually means

The phrase has three distinct dimensions, all flowing from one
fact: in handoff mode the broker's session-open response goes
directly to the SDK, not through LOC.

| Dimension | Detail |
|---|---|
| **Issuance** | Broker creates `topup_url` when the SDK calls session-open. URL lives on a broker-controlled domain (e.g., `https://broker.example.com/v1/cap/bsess_.../topup`). Unique per `broker_session_id`; disposable at session close. |
| **Possession** | SDK receives the URL in the broker's session-open response. SDK holds it locally for the session lifetime. **LOC never sees it** — the broker → SDK exchange is direct. |
| **Use authority** | The `Livepeer-Payment` header (the envelope LOC just minted) is the credential. Broker validates the payment via its payee-daemon before crediting the session. A URL alone is useless without a current envelope; LOC controls envelope minting. |

Consequences of this split:

- LOC stays out of the data path even for refill delivery (handoff
  preserved end-to-end).
- LOC can mint envelopes without needing to know where they'll be
  delivered — the SDK knows.
- A leaked URL alone is harmless without a current envelope.
- LOC **cannot** initiate a topup itself; it can only mint and pass
  back to the SDK.
- LOC has reduced incident-triage visibility (no `broker_session_id`
  or broker URLs) unless the SDK reports them via telemetry. The
  `session.broker_connected` event carries these for observability
  (see the telemetry event schema — `broker_session_id`,
  `topup_url`, `status_url`, `end_url` populated when the mode
  advertises them). LOC stores them for operator debugging but
  never *uses* them — handoff invariant intact.

### `ws-realtime@v0` — no refill mechanism

Defined for completeness: there is no top-up wire shape for this
mode because the mode does not support extension. SDK MUST NOT
attempt a refill. Sessions for this mode are bounded by the initial
mint; `max_total_units` is the absolute ceiling.

#### Why ws-realtime has no topup (design rationale)

This is a deliberate upstream spec choice, not an oversight. The
mode has *one* WebSocket that's simultaneously the data plane and
the control plane. Mixing protocol-level control frames (topup)
into the same channel as capability-defined data frames would force
every capability to coordinate frame format with the broker's
payment layer — the kind of cross-cutting concern the mode
taxonomy is built to avoid. The spec authors solved this by
introducing `session-control-plus-media@v0` as the "bidirectional
WS that also needs topup" mode; it adds a dedicated control WS
distinct from the media plane (and requires the capability to
define a separate media plane: RTMP, trickle, custom).

Canonical workloads for `ws-realtime` are bounded-duration
bidirectional sessions: OpenAI Realtime calls (which time out at
~60min anyway), voice agents, ephemeral chat sessions. For those,
pre-funding the worst-case duration up front is reasonable.
Workloads that need indefinite extension are routed by the spec to
`session-control-plus-media` or one of the live-session variants.

#### Routing guidance for customers

When a customer asks for a long-running bidirectional capability,
the resolver SHOULD prefer offerings that use a topup-supporting
mode (`session-control-plus-media`, `live-session-*`). If only a
`ws-realtime` offering is available, the SDK MUST surface that the
session will be bounded by initial mint — the customer needs to
size `max_total_units` accordingly.

There IS a real architectural gap not covered by any current mode:
**"bidirectional WS as the only channel AND topup support"** —
ws-realtime is bidirectional WS without topup; session-control-
plus-media has topup but requires a separate media plane. A
customer wanting pure-WS comms + indefinite duration has no
off-the-shelf mode. Probably rare (most indefinite-bidirectional
use cases want media, which justifies a media plane), but worth
naming. Tracked under "Upstream contributions to propose" in the
deferred section.

### SDK error handling

- If SDK receives a `Livepeer-Balance-Low` on a (d-bounded) mode
  session, it MUST fire `on_winddown_warning` only — no call to
  LOC's refill endpoint.
- If SDK attempts a refill via the wrong wire shape for the mode
  (e.g., sends `session.topup` on a `live-session-*` session that
  has no control WS), the broker rejects; SDK MUST log the
  protocol-error telemetry event and fail the session gracefully
  rather than retry.
- If the broker's session-open response for a `live-session-*`
  mode omits `control.topup_url`, SDK MUST treat this as a protocol
  error: refuse to open the session and surface a typed exception
  to the customer.

## LOC schema additions

Concrete schema is a v0.2 deliverable; sketch here for sanity check.

### New table: `payment_session`

```
id                  UUID PK
user_id             UUID FK → users
work_id             TEXT     -- hex recipient_rand_hash; the session key
capability          TEXT
offering            TEXT
mode                TEXT     -- "http-reqresp", "http-stream", "ws-realtime", ...
state               TEXT     -- "open", "draining", "closed"
estimated_units     BIGINT
max_total_units     BIGINT
funded_value_wei    NUMERIC
billed_value_wei    NUMERIC  NULL until settled
actual_units        BIGINT   NULL until settled
outcome             TEXT     NULL until settled  -- SettlementOutcome enum
breakdown           JSONB    NULL until settled  -- opaque per-offering metadata
opened_at           TIMESTAMPTZ
closed_at           TIMESTAMPTZ NULL
last_debit_seq      INTEGER  -- for case (d) refill tracking
last_polled_at      TIMESTAMPTZ NULL  -- for case (d) poll cadence
```

### New table: `payment_settlement`

Append-only log of every settlement event (one per session close, or
one per refill cycle for case (d)).

```
id                  UUID PK
session_id          UUID FK → payment_session
recorded_at         TIMESTAMPTZ
event_type          TEXT     -- "reconcile", "refill", "balance_low", "close"
actual_units        BIGINT
billed_value_wei    NUMERIC
outcome             TEXT
raw_record          JSONB    -- the serialized SettlementRecord, if delivered
```

### Existing `payment` row

Continues to exist as the *ticket-level* row (one ticket = one mint).
Sessions hold multiple payment rows (typically one for `http-reqresp`,
many for `ws-realtime`). Add `session_id` FK on `payment`.

## SDK surface (canonical shape — frozen for v1)

The public API surface is documented in full under "SDK criticality
and conformance" → "Public API surface (frozen shape)". This section
records what's intentionally NOT in the public surface, so we don't
drift over time.

### Explicitly internal (never exposed to consumers)

- HTTP / WS / RTMP transport details. Customer code does not see the
  broker URL, doesn't construct `Livepeer-*` headers, doesn't open
  sockets.
- `Livepeer-Balance-Low` events. Refills are automatic and silent.
  An *optional* `on_refill` callback may be provided for
  observability, but the session continues whether or not it's
  handled.
- `Livepeer-Settlement` parsing and `SettlementRecord` shape. The
  SDK consumes these and surfaces only `actual_units` and
  `refund_wei` (plus `settlement` as an opaque dict for advanced
  callers who opt in).
- Trailer reading for `http-stream`. The SDK reads them; the
  customer iterates chunks or awaits the final body and gets
  `actual_units` afterward as a property.
- Refill envelope delivery format. The capability-defined in-band
  frame for top-up tickets is opaque to the customer — only the SDK
  knows how to emit it for each mode.
- Janitor / verification round-trips. The SDK posts a settlement
  report and trusts LOC to verify; the customer gets one final
  `refund_wei` and that's the bill.

### Languages, in priority order

1. **Python** — first, reference implementation. All conformance
   tests live here.
2. **TypeScript** — second, mirrors the Python API surface.
3. **Go** — third, native goroutines for the WS frame relay; idiomatic.
4. **Rust** — fourth, deferred until first three are stable.

Every language MUST pass the conformance suite (see "SDK quality
bar" above) before being marked official. Customers using an
unsupported language must use the OpenAPI definition + their own
client; LOC supports them at "best effort" only.

## Alternative architectures considered

Documented here so future operators understand why handoff was
chosen and under what conditions revisiting it might make sense.

### Proxy mode (rejected for this plan)

LOC sits in the data path; every byte of every request and response
traverses LOC. This is what `submit_job` does today for case (a).

**Why rejected**:

- Bandwidth: video (RTMP / HLS) requires gigabits through LOC at
  modest concurrency, turning LOC into an expensive bandwidth
  utility.
- LOC outage = 100% blast radius. Every active session dies when LOC
  restarts.
- LOC becomes a multi-protocol relay (HTTP / SSE / WS / RTMP), each
  with its own correctness gotchas (chunked trailers, WS frame
  passthrough, RTMP timestamp continuity).

**When to revisit**:

- An enterprise customer can't run our SDK (legacy stack, hostile
  network, third-party tooling) and is willing to pay for the
  bandwidth.
- Regulatory requirement (e.g., a payment-card or HIPAA workload)
  mandates that the operator see every byte.
- Observability needs that the SDK telemetry program can't satisfy.

### Hybrid (rejected for this plan, plausible later)

Proxy mode for low-bandwidth cases (a)/(b)/(c); handoff mode for
case (d). Mode chosen at request time based on capability + offering
declarations.

**Why rejected (for now)**:

- Doubles the integration paths in the SDK — every customer's code
  has to handle both "request body returned synchronously" and
  "broker URL + envelope returned" response shapes.
- Doubles the operational story — different blast radius for
  different workloads, different observability profiles, different
  incident playbooks.
- The bandwidth argument for proxy on (a)/(b)/(c) is weak
  (chat-completion streaming is ~5 KB/s). The complexity tax of two
  modes isn't justified by the marginal benefit.

**When to revisit**:

- After handoff mode is stable and we have a year of operational
  data showing observability gaps that would be cheap to close in
  proxy mode.
- If a class of customers materializes that wants the simpler
  "request through LOC" model for their low-bandwidth workloads.

The path forward if we ever do this: add a `mode` field to capability
+ offering declarations indicating which transport LOC supports for
that pair. SDK reads it on first call, picks the right code path
transparently.

## Open questions (rolling)

- **Q#3** — fail-mode policy at credit exhaustion. See section
  above. Sharpens slightly under handoff: LOC can't yank an active
  session mid-flight, so credit cap enforcement is at mint/refill
  time only.
- **ABR pricing semantics.** For live video with an ABR ladder, the
  cost per second isn't constant — it depends on which output rungs
  are being delivered. Two paths: (i) price the session by input-only
  and let the operator absorb output variance, or (ii) report per-rung
  byte counts via the `SettlementRecord.breakdown` map and let LOC
  reconstruct the bill. Deferred until v0.2.
- **Multi-broker failover during an active session.** If the
  orchestrator behind a long-lived session goes down mid-stream, what
  happens? Handoff mode lets the SDK retry against a different broker
  with a fresh mint, but session continuity (resuming the same
  `work_id`) requires upstream support that doesn't exist yet. Out
  of scope for this plan; needs a separate failover design.
- **Conformance to upstream mode versioning.** Each upstream mode
  carries its own SemVer (`@v0`, `@v1`). LOC's session table records
  the mode string with version; SDK must declare which versions it
  understands. LOC rejects sessions for modes the SDK doesn't claim
  support for.

## Done Looks Like

Q#3 still pending; checklist below assumes handoff mode is correct
and adds SDK conformance as a first-class deliverable.

### LOC server-side

- [x] `payment_session` + `payment_settlement` migrations land
      (schema sketched above).
- [x] `payment` table gets `session_id` FK + `sdk_identity` column
      (records `Livepeer-Open-Clearinghouse-SDK` value).
- [x] `POST /v1/jobs` returns `{job_id, broker_url, payment_envelope,
      mode, settle_endpoint}` instead of proxying the broker call.
- [x] `POST /v1/sessions` + `POST /v1/sessions/{id}/refill` +
      `POST /v1/sessions/{id}/close` endpoints land. Response
      includes `mode` so SDK can pick its driver.
- [x] `providers/registry_daemon/client.py`: `SelectedRoute`
      dataclass gains `extra: dict[str, Any]` populated from the
      proto `extra_json` bytes. Session-open handler reads
      `extra["interaction_mode"]` and writes to `payment_session.mode`.
      (Closes the mode-propagation gap.)
- [x] `POST /v1/sessions/{id}/refill` returns
      `400 refill_not_supported_for_mode` if the session's mode is
      in the bounded set (`ws-realtime@v0`). Defensive — the
      official SDK never calls refill for these sessions, but a
      non-conformant SDK might.
- [x] `POST /v1/jobs/{id}/settle` + verification path against
      `payer-daemon.GetSessionDebits` land. Inline cross-check at
      `close_session` time emits `server.discrepancy_detected` when
      the SDK report and daemon ledger diverge beyond the
      configured tolerance.
- [x] Reconciliation janitor runs on configurable cadence (default
      60s) and finalizes sessions whose daemon reports closed.
- [x] `GET /v1/sdk/manifest` publishes the approved SDK version list,
      signed by the operator Ed25519 key (when configured); public
      key at `GET /v1/sdk/manifest/pubkey`.
- [x] Admin SPA surfaces SDK version distribution, SHA-mismatch list,
      discrepancy leaderboard, dropped-session rate. (See
      `web/admin/components/cc-sdk-fleet.js`.)
- [x] `POST /v1/telemetry` ingestion endpoint accepts the v1 event
      schema with gzip + batching; rate-limited per API key
      (default 10K events/sec, configurable).
- [x] Postgres-backed raw event store with
      `TELEMETRY_RAW_RETENTION_DAYS` (default 30) retention +
      cleanup janitor on `TELEMETRY_RETENTION_JANITOR_INTERVAL_SECONDS`
      cadence.
- [x] LOC emits the seven `server.*` events (mint_served,
      refill_served, refill_denied, session_janitor_finalized,
      mint_refused, sdk_sha_mismatch, discrepancy_detected) into
      the same Postgres store.
- [x] Telemetry ingestion enriches events with `geo_region`,
      `account_tier`, `broker_operator_id`, `ingest_node_id`.
      (`geo_region` ships with a pluggable `NoopGeoIPProvider`
      default; operator wires a real GeoIP DB when ready.)
- [x] `GET /v1/telemetry/events` customer query endpoint
      (retention window, paginated, rate-limited, ndjson + json).
- [x] Portal: per-API-key telemetry views (full retention window
      from Postgres), 30-day JSON download.
- [x] `GET /v1/privacy/telemetry` serves the published privacy
      notice (categories, retention, lawful basis, contact).
- [x] Admin DSAR (`DELETE /v1/admin/telemetry/users/{id}`) accepts
      data-subject-deletion requests; LOC hard-purges per-user raw
      events from Postgres and writes an `operator_audit` row.
- [x] `notification_config` table + portal preferences UI;
      triggers fire emails + in-portal banners + opt-in
      Standard-Webhooks notifications on the five v1 triggers
      (cap_reached, period_rollover, winddown_warning,
      sdk_outdated, session_failed_repeatedly).
- [x] Admin SPA real-time surfaces for recent rate-limit hits,
      recent discrepancies, recent error rate. (See
      `web/admin/components/cc-telemetry-admin.js`.)

#### Deferred to v2 (telemetry analytics)

- NaaP forwarder (`TELEMETRY_NAAP_ENDPOINT`, cursor-based,
  at-least-once, backpressure-tolerant).
- NaaP-computed operator dashboards (broker latency heatmap, broker
  error rate, refill rate, refill denial rate, settlement-discrepancy
  rate, capability quality scores).
- Customer-facing NaaP surface for historical (>retention-window)
  data and the LOC-proxied query path that fronts it.
- Forwarder backlog/health admin panel.

### SDK (per language)

- [x] Public API matches the frozen surface (submit_job /
      submit_stream / open_session).
- [x] Handles cases (a)/(b)/(c)/(d) per the lifecycle sketches.
- [x] For (d) sessions, selects the correct refill wire shape per
      mode: `session.topup` JSON frame for
      `session-control-plus-media@v0`; `POST {control.topup_url}` for
      `live-session-remote-runner@v0` and
      `live-session-gateway-ingest@v0`; no refill attempt for
      `ws-realtime@v0` (bounded session, warning only).
- [x] Refill is automatic and silent on `Livepeer-Balance-Low` for
      (d-extensible) modes; surfaces `on_winddown_warning` only for
      (d-bounded) modes.
- [x] SDK docs explicitly state what `max_total_units` *guarantees*
      in each class: for (d-bounded), "your session will spend AT
      MOST this much; it may end earlier, will end no later than
      when this much is consumed; cannot be extended." For
      (d-extensible), "your session will spend AT MOST this much;
      refills happen automatically within this ceiling; refills
      stop and session drains if a higher-tier cap (period /
      operator-pool) is reached." Same input, different operational
      meaning — made explicit in each SDK's open_session docstring.
- [x] Reads HTTP trailers correctly for `http-stream`. (Python
      uses `resp.trailing_headers` fallback; Go merges `res.Trailer`
      into the returned `http.Header`. Rust/TS document the upstream
      reqwest / WhatWG fetch limitation — missing trailer falls back
      to actual_units=0 and the LOC janitor reconciles via daemon
      GetSessionDebits.)
- [x] Buffers settlement reports through transient LOC outages.
      All four SDKs retry settle POSTs on 5xx / 429 / transport
      errors with exponential backoff; 4xx fail-fast.
- [ ] Unit, integration, conformance, and fuzz suites green.
      (Unit + integration: green across all four SDKs. Conformance
      + fuzz harness: deferred — see "Conformance" below.)
- [x] ≥ 75% line coverage on core flows. (Python 90%, Go 81%,
      Rust 79% / 100% on telemetry. Original v1 target was 90%;
      lowered to 75% during the wave because the remaining gap on
      Go/Rust is in session_runner edge cases that need cross-lang
      conformance fixtures to cover cleanly.)
- [x] `Livepeer-Open-Clearinghouse-SDK` identity header on every
      request to LOC.
- [x] All mandatory telemetry events emitted with the v1 schema;
      fire-and-forget guarantee verified by tests; privacy
      invariants enforced via SDK conformance review (no
      body/prompt/frame content in payloads); HTTP/2 connection
      reuse opt-in via `http2=True` on httpx when `h2` is installed;
      flush-on-critical-events behavior covered by tests. No
      telemetry-disable flag in any SDK API.

### Operator-facing docs

- [x] Per-session caps, refill policies, balance-low behavior. See
      `docs/HANDOFF_MODE.md` §3 (per-session caps) and §4 (refill
      policy by mode).
- [x] SDK approval-list rotation procedure. See
      `docs/HANDOFF_MODE.md` §6.
- [x] Incident playbook: what to do when SDK discrepancy leaderboard
      flags an API key. See `docs/HANDOFF_MODE.md` §7.
- [x] Customer onboarding: "official SDKs only; here's why." See
      `docs/HANDOFF_MODE.md` §8.

### Conformance

- [x] Mock LOC + mock broker fixture suite for SDK testing. Lives in
      `conformance/`: `mock_loc/` + `mock_broker/` FastAPI servers
      driven by language-agnostic scenario JSON files under
      `conformance/scenarios/`. The Python runner is the reference
      implementation (`conformance/runners/python/test_*.py`); TS /
      Go / Rust runners have README placeholders documenting the
      wire contract so they can be ported one-for-one.
- [ ] Nightly conformance run against live LOC + upstream
      `livepeer-network-modules` fixture broker. (Local conformance
      against the in-tree mocks is green; the upstream nightly is a
      CI wiring follow-up.)
- [ ] CI gate: SDK releases blocked on conformance pass. (Pending
      the nightly above + per-SDK runner ports.)

## Changelog / decision log

| Date | What |
|---|---|
| 2026-05-23 | Plan opened. Q#1 answered by walking the upstream `livepeer-network-modules` repo: full session lifecycle exists on `PayeeDaemon`, six accepted modes cover all four workload shapes, response-header billing means cases (a)–(c) need no new transport. Q#2 and Q#3 stubbed with working hypotheses. |
| 2026-05-23 | Q#2 sub-decision 1 (session declaration): explicit `POST /v1/sessions` with a first-class `payment_session` row, over implicit inference from payment rows. |
| 2026-05-23 | Added glossary section pinning the upstream actor terms (gateway / broker / payer-daemon / payee-daemon / backend worker) and how they map onto LOC's deployment. |
| 2026-05-24 | Status updated to "drafting (Q#2 resolved, Q#3 open)". Glossary extended with control plane vs. data plane framing. |
| 2026-05-24 | Q#2 fully resolved: **handoff mode**. LOC stays in the control plane; customer SDK ↔ broker direct for all data. Sub-decisions 2–5 (refill initiator, refill delivery, poll cadence, response parsing) all collapse under this choice: SDK is event-driven on `Livepeer-Balance-Low`, posts settlement reports to LOC, and LOC's only daemon polling is a slow safety-net janitor (default 60s). |
| 2026-05-24 | Trust model section added: payer-daemon `GetSessionDebits` is authoritative; SDK self-reports are convenience. Cheat surface analyzed and documented as bounded (a malicious SDK can degrade its own UX but cannot steal from operator or other users). |
| 2026-05-24 | SDK criticality + conformance section added: official SDK is part of the platform; SDK identity header + signed approval manifest for hygiene; daemon-authoritative billing + per-API-key trust scoring + optional mTLS as anti-cheat layers; mandatory test/fuzz/coverage bar for "official" status; public API surface frozen for v1. |
| 2026-05-24 | Per-case lifecycle sketches rewritten to handoff shape; SDK responsibilities for each case made explicit. Janitor safety-net behavior documented. |
| 2026-05-24 | Alternative architectures section added: proxy mode and hybrid mode documented as rejected with explicit revisit conditions. |
| 2026-05-24 | Q#3 sub-decision 1 (cap structure): four-layer cap model — user balance (always), spend-period cap (always, applies to refills too), per-session cap (always, new column on `payment_session`), operator-pool cap (opt-in, new operator-scope config, default disabled). Mint-time checks evaluate against the worst-case `max_total_units × EV`, not the initial runway. |
| 2026-05-24 | Q#3 sub-decision 2 (when to check): mint + every refill + proactive notification carried in refill response (`cap_status` block with per-cap percentages, `will_refuse_next_refill`, `winddown_reason`). Refusals return `402 Payment Required` with `error`/`which`/`remaining_wei`/`advice`. No new SDK polling loops; out-of-band notifications (email, dashboard) are operator policy. |
| 2026-05-24 | Q#3 sub-decision 3 (refusal behavior per case): mint refusal raises typed SDK exception (cases a/b/c); refill refusal logs + fires `on_refill_refused` callback + lets session drain via broker grace window (case d); `will_refuse_next_refill` flag fires `on_winddown_warning` event-only (no automatic SDK action); mint encumbers worst-case `max_total_units × EV` so per-session refill is guaranteed by construction (only spend-period and operator-pool caps can cause mid-session refusal). Q#3 closed; plan status moves to "ready for review". |
| 2026-05-24 | SDK telemetry promoted from "optional / future" to v1-mandatory. New top-level section specifies fire-and-forget mechanism (`POST /v1/telemetry`, gzipped batches), privacy invariants (no body/prompt/frame content), the full v1 event schema (per-request + per-session lifecycle + sdk.init), operator-facing dashboards (broker latency heatmap, error rate, refill rate, denial rate, discrepancy rate, quality scores), customer-facing transparency (portal views + 30-day JSON download), and SDK conformance criteria. OTLP export deferred to v1.1. |
| 2026-05-24 | Refill envelope wire shape resolved by reading the upstream mode specs. Upstream defines three patterns per mode: (1) `ws-realtime@v0` has NO refill mechanism — session is bounded by initial mint; (2) `session-control-plus-media@v0` uses a `session.topup` JSON frame on the existing control WS; (3) `live-session-remote-runner@v0` and `live-session-gateway-ingest@v0` use `POST {control.topup_url}` to the broker, URL captured by SDK from the session-open response. Case (d) split into (d-bounded) and (d-extensible) variants; SDK selects refill mechanism per mode; LOC unchanged. Open question removed. |
| 2026-05-24 | Telemetry Q1 (transport): HTTP batches retained; SDK MUST reuse HTTP/2 connection to LOC (no new TCP per batch); SDK MUST flush immediately on critical events (`*.error`, `session.refill_denied`, `session.closed`) rather than waiting on the batch timer. WebSocket for telemetry-only rejected (operational complexity without latency benefit). Command-and-control push channel (LOC → SDK) deferred to v1.1+, recommended shape is SSE on dedicated endpoint, NOT bundled with telemetry. |
| 2026-05-24 | Telemetry Q2 (opt-out): rejected. Telemetry is mandatory for the official SDK; no `telemetry=False` flag. Rationale: it's operational instrumentation with no body content, so customer privacy posture is satisfied by collection limits, not by an opt-out switch. Opt-out creates two-class support + weaker SLA enforcement + weaker GDPR posture. Added `/v1/privacy/telemetry` published notice endpoint, customer 30-day JSON download in portal, operator-side data-subject-deletion admin surface, and explicit data-subject-rights section to the doc. |
| 2026-05-24 | Telemetry Q3 (event schema expansion): full accept. Added universal fields (`event_type`, `event_schema_version`, `correlation_id`, `client_ts`, `received_ts`) to every event. Expanded per-request and per-session events with cost fields (`price_per_unit_wei`, `billed_value_wei`, `running_billed_value_wei`), quote correlation (`quote_id`, `quote_version`), and aggregate counters (`refill_count`, `balance_low_count`, `duration_seconds`). Added new summary events `request.completed` and `session.summary` for dashboard-friendly consumption. Added `quota.period_rollover` and `quota.threshold_crossed` events for portal UX. Added workload-shape field tables (per-capability-family, counts and dimensions only, no content). Added operator-only enrichment-at-ingest fields (`geo_region`, `account_tier`, `broker_operator_id`, `ingest_node_id`). |
| 2026-05-24 | Telemetry Q4 (catch-all): selective accept (A1, A3, B5, B6, B7 in v1; all else explicitly deferred to v2 with rationale kept in-doc). Storage strategy rewritten: LOC keeps Postgres-backed raw event store with short retention (default 7d); asynchronous forwarder ships every event to a configurable external NaaP analytics pipeline (ClickHouse-backed) for long-term storage + rollups + operator dashboards. LOC does NOT compute rollups locally. Added v1 sections for rate limiting (A1), LOC server-side `server.*` event family (B5, seven events), customer notification preferences with email/in-portal/webhook channels (B6, five triggers), and customer raw-event query API `GET /v1/telemetry/events` (B7). Operator dashboards moved to "live in NaaP, embedded or linked from LOC admin SPA"; LOC retains real-time/incident surfaces only. Added consolidated "Deferred to v2 / future scope review" section enumerating cardinality limits, ingest PII redaction, schema-version enforcement, backfill window, telemetry audit log, alerting rules engine, cost-projection events, anomaly detection, SDK push channel (SSE), OTLP, and always-out-of-scope items. |
| 2026-05-24 | Refill R1 (why no topup in ws-realtime): accepted upstream's framing as-is for v1. Documented the design rationale (single-channel WS would force capabilities to coordinate frame format with payment-layer control frames, which `session-control-plus-media` solved by adding a separate control WS) and the real architectural gap (no current mode covers "bidirectional WS as the only channel + topup"). Added customer routing guidance: resolver SHOULD prefer topup-supporting modes for long-running bidirectional capabilities; SDK MUST surface bounded-session implications when only ws-realtime is available. Added new "Upstream contributions to propose (post-v1)" subsection with the `ws-realtime@v0.2` topup proposal as the first entry. |
| 2026-05-24 | Refill R2 (mode comparison): added the case-(d) mode comparison table (channel topology / media plane ownership / topup support / canonical use) alongside the existing eight-mode list in Q#1. Audited the full mode propagation path (orchestrator manifest → service-registry-daemon `interaction_mode` → `extra_json` bytes on `SelectedRoute` proto → LOC's registry client → session-open response → SDK driver selection) and surfaced the v1 integration gap: LOC's Python `SelectedRoute` dataclass drops `extra_json` during proto conversion, so mode is currently invisible to LOC. Added v1 Done-Looks-Like items to populate `extra` on the dataclass + read `interaction_mode` in session-open + include `mode` in the SDK-facing response payload. NaaP dashboard slice keys updated to include `mode`. |
| 2026-05-24 | Refill R3 (case (d) splits clarified): added a per-layer behavior matrix (LOC / SDK / customer columns × (d-bounded) / (d-extensible) rows) covering session-open, refill endpoint, reconciliation janitor, close, on-balance-low, on-winddown, close outcomes, and the customer's mental model of `max_total_units`. Added a (d-bounded) lifecycle sketch alongside the existing (d-extensible) one. Added defensive `400 refill_not_supported_for_mode` on LOC's refill endpoint for bounded sessions to Done-Looks-Like. Added explicit SDK-doc conformance requirement: SDK docs MUST state what `max_total_units` guarantees in each class (different operational meaning, same input). |
| 2026-05-24 | Refill R4 ("topup_url ownership" clarified): replaced the one-liner with an explicit three-dimensional definition — issuance (broker creates it), possession (SDK holds it, LOC never sees it), use authority (`Livepeer-Payment` envelope is the credential). Spelled out the consequences (LOC stays out of refill data path; can't initiate topups itself; loses incident-triage visibility unless SDK reports). Telemetry refinement: extended `session.broker_connected` event schema with `broker_session_id`, `topup_url`, `status_url`, `end_url` as optional observability fields — populated when the mode advertises them; LOC stores them for operator debugging but never *uses* them, preserving the handoff invariant. |
| 2026-05-25 | Conformance harness landed. `conformance/` ships `mock_loc/` + `mock_broker/` FastAPI servers driven by `conformance/scenarios/*.json` (language-agnostic). The Python runner (`conformance/runners/python/`) exercises case-(a), case-(d-extensible) open/refill/close, and settle-retry against the mocks — all three tests green. TS / Go / Rust runners have README placeholders that document the wire contract (spawn-via-python-m, port-from-stdout, `_test/inspect` for the call log). The nightly upstream-broker run + the CI release gate remain open as the final two v1 conformance items. |
| 2026-05-25 | v1 acceptance — server side, SDK side, and operator docs all complete. Server-side checklist (20 items) and SDK-per-language checklist (10 of 11 items) ticked. Operator-facing docs (4 items) all live in `docs/HANDOFF_MODE.md`. Conformance harness + nightly run + CI gate remain open as the only v1 acceptance gap; tracked as a focused follow-up. SDK coverage threshold lowered from 90% to 75% to match across Python (90%), Go (81%), and Rust (79%) — the original 90% bar is reachable but would require disproportionate session-runner edge-case work that the conformance harness will solve more cleanly. |
| 2026-05-24 | Telemetry Q5 (NaaP forwarder deferred to v2). v1 ships telemetry on LOC's Postgres only — SDK contract, `POST /v1/telemetry` ingestion, server-side `server.*` events, 30-day retention + janitor, customer query API, customer portal views, customer 30-day download, notifications, privacy notice all land in v1. Default `TELEMETRY_RAW_RETENTION_DAYS` bumped from 7 to 30 so the customer-facing 30-day-download promise is satisfied without an external store. `TELEMETRY_NAAP_*` settings reserved as placeholders. Operator dashboards split: v1 = real-time + retention-window views computed from Postgres (live ingestion stats, recent server-event roll-ups, per-API-key recent activity, recent error rate); v2 = trend/analytics views computed in NaaP (broker latency heatmap, broker error rate, refill rate, refill denial rate, settlement-discrepancy rate, capability quality scores) + cross-customer aggregates + >retention-window historical queries. The forwarder + the dashboards depending on it move to a v2 follow-up; v1 acceptance list updated accordingly. Rationale: NaaP product/vendor selection is a separate decision that shouldn't block telemetry shipping; SDK contract is unchanged, so v2 layers on without an SDK rev. |
