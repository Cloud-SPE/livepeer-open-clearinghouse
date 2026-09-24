# proto/

Vendored protobuf definitions for the daemons Livepeer Open Clearinghouse integrates with.

Source: `/livepeer-cloud-spe/livepeer-network-modules/livepeer-network-protocol`.

Vendored verbatim from Modules commit
`d8d369de4aba36c397c4f758f668239b6c09a206` (Network Protocol `4.0.0`, `wholesale-account` `2.0.0-draft`).
The wire schema is unchanged from the settlement-domain integration baseline
`80800f8be422b5ed08c6fe65a65611f67131cbfd`; later upstream edits were
comment-only on the payer and registry surfaces. Registry protos come from
`proto-contracts/livepeer/registry/v1/`. Re-vendoring requires recording a
new exact Modules revision; daemon image tags alone are not a schema identity.

Registry definitions and catalog fixtures were subsequently updated verbatim from
Modules commit `e9f08e4fdc934567fa0d2c4979f07773a4180aa8` (`proto-contracts`).
LOC now requires the additive `Resolver.ListOfferings` RPC from that revision or
later; `UNIMPLEMENTED` fails closed and does not trigger address crawling.
Payment definitions retain the baseline above.

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
