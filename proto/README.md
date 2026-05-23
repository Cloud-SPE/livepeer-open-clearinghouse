# proto/

Vendored protobuf definitions for the daemons Livepeer Open Clearinghouse integrates with.

Source: `/livepeer-cloud-spe/livepeer-network-modules/livepeer-network-protocol`.

## Layout

```
proto/livepeer/payments/v1/
├── payer_daemon.proto   # the PayerDaemon gRPC service
└── types.proto          # shared messages (Payment, TicketParams, QuoteRef, ...)
```

## Regenerating Python stubs

```
make protoc
```

This compiles every `proto/**/*.proto` into Python modules under
`src/livepeer_open_clearinghouse/providers/payment_daemon/_gen/`. The generated files are
committed so the runtime image doesn't need `grpcio-tools` at build time.

## When to re-vendor

When the upstream `livepeer-network-protocol` releases a new
schema-affecting version, copy the two files above and run `make protoc`.
The wire-compat contract on `Payment` is stable; the daemon-consumer
contract on `PayerDaemon` may evolve under v1 (see `docs/wire-compat.md`
in the upstream repo).
