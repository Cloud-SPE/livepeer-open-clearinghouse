# proto/

Vendored protobuf definitions for the daemons Livepeer Open Clearinghouse integrates with.

Source: `/livepeer-cloud-spe/livepeer-network-modules/livepeer-network-protocol`.

The payments `types.proto` and `payer_daemon.proto` are a coordinated candidate
from Modules branch `feat/shared-wallet-isolation`, commit `50df55314aa84646fc4ccdc094a85ee1424a181e`.
They introduce wholesale-account `3.0.0-draft` and authorization signing domain
`v3`. The vendored content is also pinned by SHA256:

- `types.proto`: `d97b96526830b2b87bacf1eeb9a5b5e665e5584fb5b846161827f42cd1aadf66`
- `payer_daemon.proto`: `2c66f61c40ed38261433a4cd67500f02f01deb46738eead4e5976e10c87a1445`

Registry protos remain from `proto-contracts/livepeer/registry/v1/` at the
previous Modules revision `d8d369de4aba36c397c4f758f668239b6c09a206`.
Daemon image tags alone are not a schema identity.

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
