# PymtHouse

A non-custodial-by-design payment clearinghouse for Livepeer applications.

PymtHouse authenticates app developers, manages their wei-denominated credit
balance, and mints signed Livepeer payment tickets on their behalf. App
developers integrate with one HTTP API and never touch a signing key.

## Status

Pre-alpha. Scaffolding in progress.

## How to run it locally

PymtHouse runs as a Docker Compose stack with four services:

- `pymthouse-gateway` — this repo (Python / FastAPI)
- `db` — Postgres 16
- `payment-daemon` — pre-built image from `livepeer-network-modules`
- `service-registry-daemon` — pre-built image from `livepeer-network-modules`

```bash
cp .env.example .env
make dev
```

The first time, you'll also need to generate a dev keystore for the
payment-daemon:

```bash
./scripts/dev-keystore.sh
```

The portal is at <http://localhost:8000/portal/>, the admin console at
<http://localhost:8000/admin/>, and the OpenAPI docs at
<http://localhost:8000/docs>.

## How it fits together

```
app dev → pymthouse-gateway → service-registry-daemon (discovery)
                            → payment-daemon (mint tickets)
                            → postgres (users, credit, usage)
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the domain layout and
[`AGENTS.md`](AGENTS.md) for how to navigate the codebase.

## Documentation

| Where | What |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Repo map and working principles |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Domain + layer architecture |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Load-bearing design decisions |
| [`docs/PRODUCT_SENSE.md`](docs/PRODUCT_SENSE.md) | Product mission and scope |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Key custody, secrets, auth model |
| [`docs/`](docs/) | Full documentation tree |

## License

MIT. See [`LICENSE`](LICENSE).
