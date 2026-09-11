# proto/

Vendored protobuf definitions for the daemons Livepeer Open Clearinghouse integrates with.

Source: `/livepeer-cloud-spe/livepeer-network-modules/livepeer-network-protocol`.

Payments baseline: Modules commit
`913cf7de10e5c090fd60ccc36234943210670f0d`, including the
`wholesale-account` `1.0.0-draft` contract. Re-vendoring requires recording a
new exact Modules revision; daemon image tags alone are not a schema identity.

## Layout

```
proto/livepeer/payments/v1/
├── payer_daemon.proto   # the PayerDaemon gRPC service
└── types.proto          # shared payment, account, authorization, settlement messages
```

## Regenerating Python stubs

```
make protoc
```

This compiles every `proto/**/*.proto` into Python modules under
`src/livepeer_open_clearinghouse/_gen/`. The generated files are
committed so the runtime image doesn't need `grpcio-tools` at build time.

## When to re-vendor

When the upstream `livepeer-network-protocol` releases a new
schema-affecting version, copy the two files above and run `make protoc`.
The wire-compat contract on `Payment` is stable; the daemon-consumer
contract on `PayerDaemon` may evolve under v1 (see `docs/wire-compat.md`
in the upstream repo).
