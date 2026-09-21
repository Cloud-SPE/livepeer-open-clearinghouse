# Shared-wallet account isolation

LOC uses an explicit, stable `WHOLESALE_ACCOUNT_ID` to distinguish its wholesale
credit from other applications and environments sharing its wallet. Use
`loc-prod` for production and a distinct `loc-dev-<installation>` for each
independent development installation. The label is 1–128 ASCII letters,
digits, dots, underscores, colons, or hyphens. Startup with a real payer daemon
requires the setting; the Compose files require it as well.

The account coordinate is `(chain, payer, payee, settlement_domain_id,
wholesale_account_id, denomination)`. The local `WholesaleFunding.account_id`
remains a UUID foreign key; it is not the protocol label. Customer balances and
API keys remain in LOC's separate retail ledger.

The Modules payer daemon owns and persists `ticket_stream_id` in its database.
LOC neither generates nor configures that identity. Every independent daemon
needs its own persistent database; never clone an active daemon's database into
another simultaneously running installation. Account labels remain stable
across restarts. A different label opens a different account, without moving
credit from the old one.

## Protocol boundary

All account/status requests carry `wholesale_account_id`. Account funding sends
`Livepeer-Wholesale-Account-Id`, and the signed funding intent binds the same
account. LOC requires `isolation_version=1` and the matching account in broker
responses. Authorizations use `livepeer-spend-authorization/v3`; LOC checks the
account in signed authorizations, settlements, and non-admission audit evidence.
The account is persisted with each authorization so recovery never substitutes
a newly configured account.

A funding receipt identifies `SHA256(exact payment_bytes)` as lowercase hex
`funding_id`. The original credited amount and original account snapshot are
replayed. LOC checks the digest and minted value, then requires a current
observation at or after the receipt's version. A later account balance need not
match the historical receipt balance. No cumulative balance delta is attributed
to this funding operation. Missing fields and old peers fail closed; there is
no wallet-wide fallback.

## Migration and deployment

Upgrade the serving brokers, receiver daemons, and payer daemon together with
this LOC client. Existing image tags are intentionally unchanged by this source
change; release matching images before deployment. Do not point this client at
a pre-isolation release.

Before applying migration `0029`, stop new work/funding and drain and reconcile
legacy authorizations and funding attempts. The migration refuses pending
funding and nonterminal grants. Existing account balances and grants retain an
empty account label for audit, rather than being assigned to LOC or duplicated
into a new account. Existing Protocol 4 account balances still count toward
LOC's conservative exposure accounting. Reconcile and drain legacy broker
credit explicitly before the cutover; this migration does not transfer it.
The migration cannot be downgraded by collapsing isolated accounts together.

A shared wallet still shares one on-chain deposit and signing authority. Labels
prevent accidental accounting and nonce interference, not spending by another
holder of the private key. Development can still consume the shared deposit.
Enforced tenant budgets require a common signer or separate wallets.

Tracked by `loc-ddc`. See [fair wholesale accounts](002-fair-wholesale-credit-accounts.md).
