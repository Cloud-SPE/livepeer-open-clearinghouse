# Reference: payment-daemon

A reference card for the `payment-daemon` we integrate with. Source:
`/home/mazup/git-repos/livepeer-cloud-spe/livepeer-network-modules/payment-daemon`.

This is a digest for fast lookup. When the daemon's behavior is the
authority, go read the daemon's source — links at the bottom.

## What it is

A Go sidecar that encapsulates Livepeer probabilistic-micropayment state
and signing. One binary, two roles selected by `--mode`:

- **sender** — mints/signs `Payment` envelopes (one or more tickets) for a
  paying party. This is what Livepeer Open Clearinghouse uses.
- **receiver** — orchestrator-side; validates incoming payments, redeems
  winners on-chain.

Livepeer Open Clearinghouse only ever talks to the sender mode.

## Transport

- **gRPC over a Unix domain socket.**
- Default socket: `/var/run/livepeer/payer-daemon.sock`.
- Socket mode: `0o660`. Trust is filesystem-mediated; any process with
  matching uid/gid can call any RPC.
- **No auth on the gRPC surface for sender RPCs.** Caller is implicitly
  trusted because it has socket access.
- Service: `livepeer.payments.v1.PayerDaemon`.

Livepeer Open Clearinghouse mounts the same `livepeer-run` volume as the daemon and runs as
uid/gid `65532:65532`.

## The load-bearing RPC

### `CreatePayment(CreatePaymentRequest) → CreatePaymentResponse`

**Request:**

```
recipient: bytes (20-byte ETH address of orchestrator)
ticket_params_base_url: string (broker URL the daemon will POST against
                                 to fetch authoritative TicketParams)
accepted_price: AcceptedPrice {
    capability: string
    offering: string
    price_per_unit_wei: string (decimal big-int)
    units_per_price: uint64
    work_unit_name: string
    quote_ref: QuoteRef {
        quote_id: string                   # all three are non-empty required
        constraint_fingerprint: bytes
        route_fingerprint: bytes
    }
}
funding: FundingIntent {
    funded_value_wei: BigUInt
    estimated_units: uint64
    max_total_units: uint64
}
```

**Response:**

```
payment_bytes: bytes               # base64 this into the
                                   #   Livepeer-Payment HTTP header
tickets_created: uint32            # always 1 in current daemon
expected_value: BigUInt            # EV in wei — what to charge the user
funded_value_wei: BigUInt          # echoes the funding intent
accepted_quote_ref: QuoteRef
work_id: string                    # an opaque session key from the daemon
```

**Behavior:**

- Daemon fetches `TicketParams` from `${ticket_params_base_url}/v1/payment/ticket-params`
  (synchronous outbound HTTP, 5s timeout) before signing.
- Daemon signs **one ticket per call**. If Livepeer Open Clearinghouse needs N tickets, it
  calls N times.
- Daemon caches session keyed by
  `(recipient, capability, offering, funded_value_wei, ticket_params_base_url)`
  so repeated calls reuse `recipient_rand_hash` and increment nonce.
- EV is `face_value × win_prob / 2^256`, computed by the daemon and
  returned in `expected_value`. The caller does not compute EV.
- `face_value` from `funding.funded_value_wei` is a **request**, not
  authoritative — the receiver chooses the final `face_value` × `win_prob`
  pair. EV in the response is the authoritative value.
- `quote_ref.quote_id`, `constraint_fingerprint`, `route_fingerprint` are
  validated as non-empty. Livepeer Open Clearinghouse gets all three from
  `service-registry-daemon.Select()`.

**Errors Livepeer Open Clearinghouse must handle:**

- `codes.Aborted` on `ReportPaymentResult` → semantic "session rotated,
  retry once." Metadata carries old `work_id`.
- Plain `errors.New` strings from sender validation when deposit/reserve
  is zero or `WithdrawRound` is imminent → surface as
  `503 DAEMON_DEPOSIT_INSUFFICIENT`.
- Anything else from the daemon → `503` to the caller.

## Other RPCs

| RPC | Use |
|---|---|
| `ReportPaymentResult` | Caller reports payee rejection (`INVALID_RECIPIENT_RAND`); daemon evicts cached session. Returns `Aborted` with retry-once metadata. |
| `GetDepositInfo` | Read TicketBroker deposit/reserve/withdraw_round for the hot wallet. Useful for admin/health surfaces. |
| `GetSessionDebits` | Long-running session debit ledger. May be `UNIMPLEMENTED`. |
| `Health` | Returns `"ok"`. |

Receiver-side RPCs (`PayeeDaemon`) exist but are not on the sender socket;
Livepeer Open Clearinghouse never calls them.

## Key custody

- One Ethereum signing key per daemon process.
- Loaded at boot from a go-ethereum V3 JSON keystore via `--keystore-path`.
- Decrypted using `--keystore-password-file` or
  `LIVEPEER_KEYSTORE_PASSWORD` (mutually exclusive).
- Held in memory as `*ecdsa.PrivateKey`. Never logged. Password buffer
  zeroed after decrypt.
- No KMS/HSM/remote-signer hook in the current implementation.

**For Livepeer Open Clearinghouse's pooled-wallet model: one Livepeer Open Clearinghouse instance ↔ one
payment-daemon ↔ one pooled wallet.** This is the explicit constraint.

## Ticket data model

```
Ticket {
    Recipient: 20 bytes (orchestrator)
    Sender: 20 bytes (this daemon's wallet)
    FaceValue: *big.Int (wei)
    WinProb: *big.Int (scaled to 2^256)
    SenderNonce: uint32 (per-session monotonic, capped ~600)
    RecipientRandHash: 32 bytes (session key)
    CreationRound: int64
    CreationRoundHash: 32 bytes
}
Signature: 65 bytes (R || S || V), EIP-191 over the ticket hash
```

No explicit expiry timestamp on the ticket; freshness is via
`CreationRound` vs `--validity-window`.

## Config (sender mode)

| Flag / env | Required? | What |
|---|---|---|
| `--mode=sender` | required | daemon mode |
| `--socket` | default `/var/run/livepeer/payer-daemon.sock` | UDS path |
| `--chain-rpc` | required for prod | Ethereum JSON-RPC (Arbitrum) |
| `--keystore-path` | required for prod | V3 keystore file |
| `--keystore-password-file` *or* `LIVEPEER_KEYSTORE_PASSWORD` | required for prod | keystore password |
| `--dev-signing-key-hex` | dev only | raw hex key, rejected when `--chain-rpc` is set |
| `--orch-address` | optional | cold orch identity (recipient embedded in tickets) |
| `--chain-controller-address` | optional | override Controller address |
| `--expected-chain-id` | default Arbitrum One | sanity-check |

Sender mode does not use a DB; sessions are in-memory.

## Deployment

- Dockerfile present (multi-stage, distroless nonroot).
- Image: `tztcloud/livepeer-payment-daemon:vX.Y.Z` on Docker Hub.
- `compose/docker-compose.yml` defines `payment-daemon-sender` and
  `-receiver` profiles.
- Mounts `PAYMENT_DAEMON_SOCKET_DIR` (default `/var/run/livepeer`) as the
  socket volume. Keystore + password mounted read-only.

Livepeer Open Clearinghouse deploys as a peer container sharing the socket-dir volume.

## Gotchas

- **One ticket per call.** Need N → call N times.
- **`ticket_params_base_url` is required per call.** Livepeer Open Clearinghouse must know
  the orchestrator's broker URL (it comes from
  `service-registry-daemon.Select().worker_url`).
- **Synchronous outbound HTTP inside `CreatePayment`** (5s timeout).
  Latency includes this round-trip.
- **`AcceptedPrice.quote_ref` is strictly validated.** Triplet must be
  non-empty. Livepeer Open Clearinghouse synthesizes/forwards from `Select()`.
- **Caller-supplied `face_value` is a request, not authoritative.** Trust
  `response.expected_value` for charging.
- **No multi-wallet support.** One daemon process, one wallet. Per-tenant
  signing would require multiple daemon processes.
- **UDS only.** No TCP, no TLS. Livepeer Open Clearinghouse and daemon must be co-located.
- **`examples/` is empty.** No reference client; we're the first.

## Key source paths

- `internal/service/sender/sender.go` — `CreatePayment` impl
- `internal/service/sender/ticketparams_fetcher.go` — broker HTTP fetch
- `internal/server/server.go` — gRPC server + socket mode
- `cmd/livepeer-payment-daemon/main.go` — flags, keystore loading
- `internal/providers/keystore/inmemory/inmemory.go` — key handling
- `livepeer-network-protocol/proto/livepeer/payments/v1/payer_daemon.proto` — wire types
- `compose/docker-compose.yml` — deployment reference
