# 07 — Multi-agent and multi-repo

Coordinating several agents, several repositories, and several machines around
one work graph.

---

## 1. Assignment vs claiming

There is no agent registry. Assignees are plain strings.

```bash
bd assign bd-42 agent-1          # a coordinator hands work out
bd update bd-42 --claim          # an agent takes work for itself (ATOMIC)
bd ready --claim --json          # take the first ready bead matching filters
bd assign bd-42 ""               # unassign
bd update bd-42 --status open    # make it claimable again
```

**Prefer `--claim` whenever agents self-select.** It sets assignee and
`in_progress` in one atomic operation: the first claim wins, and re-claiming
something you already hold is idempotent. `bd assign` followed by
`bd update --status` is two operations and races.

Seeing who is doing what:

```bash
bd list --status in_progress --json                 # the whole active set
bd list --assignee agent-1 --status in_progress
bd ready --assignee agent-1
bd ready --unassigned
```

---

## 2. Claim leases

> **Not in the 1.2.2 binary.** `bd heartbeat`, `bd reclaim`, and `bd unclaim`
> are registered on upstream `main` but 1.2.2 rejects them with
> `Error: unknown command`. This section documents the design as upstream
> describes it, for when it ships. On 1.2.2, claims are plain
> `assignee` + `in_progress` state with no TTL: detect abandoned work with
> `bd list --status in_progress` plus `bd stale`, and release it manually with
> `bd update <id> --status open` and `bd assign <id> ""`.

A claim can carry a lease with a TTL, kept alive by heartbeats and reaped when
a worker dies. Two rules matter, and they matter *most* in federated setups:

- **A lease is only meaningful on the replica that granted it.** The `leases`
  table is clone-local and never replicates. What crosses a federation bridge
  is only the claim's *visibility* (`status`/`assignee` on the issue row), and
  that is stale by up to one sync interval on every other replica.
- **Reclaim belongs to the granting replica.** Each lease records the replica
  that granted it, and the reaper skips foreign leases, naming them on stderr.
  Reap dead workers on the machine that hired them.

Consequences for configuration:

- Lease TTL **and** reclaim grace must both exceed your sync interval. The
  reaper defaults its grace to 2× the TTL; raise the TTL rather than shrinking
  the sync interval.
- Name each replica so the cross-replica guard arms:

```bash
export BEADS_NODE_ID=mini          # per machine
bd config set node_id mini         # writes ~/.config/bd/config.yaml (user-global)
```

`node_id` names the **store, not the host**. Hosts that are clients of the same
`dolt sql-server` are **one replica** — give them the same value or leave it
unset. Distinct ids for one store recreate the failure the guard exists to
prevent: a supervisor matches no worker's lease and reclaims nothing forever.

`node_id` must **never** be committed. `.beads/config.yaml` is git-tracked; a
`node_id` committed there propagates one machine's identity to every clone, at
which point every comparison matches and the guard is armed but inert — worse
than not setting it at all. That is why `bd config set node_id` writes the
user-global file. There is deliberately no hostname fallback (container
hostnames are per-run; macOS transient hostnames follow the network).

An unset identity degrades to the old behaviour (every lease treated as local)
rather than failing closed, so upgrades and single-store deployments can never
strand work.

Recovering a stranded lease — after confirming the granting replica is not
still reaping:

```bash
bd reclaim --any-replica --id <id>     # narrow form, preferred
bd unclaim --force <id>
bd reclaim --any-replica               # global: reverts EVERY foreign stale lease
```

---

## 3. Handoff patterns

### Sequential handoff

```bash
# Agent A
bd comment bd-42 "API complete. Endpoints in api/v2/. Needs frontend wiring;
see the contract in docs/api-v2.md."
bd assign bd-42 agent-b

# Agent B
bd list --assignee agent-b
bd comments bd-42            # read the handoff before starting
bd update bd-42 --claim
```

Use `bd comment` for messages between agents and `bd note` for the bead's own
running log. Both survive compaction; chat does not.

### Parallel work

```bash
bd create "Part A" --parent "$EPIC"
bd create "Part B" --parent "$EPIC"
bd create "Part C" --parent "$EPIC"

# Workers self-select:
bd ready --parent "$EPIC" --claim --json

# Coordinator watches:
bd list --status in_progress --json
bd swarm status "$EPIC"
```

### Fan-in

One dependency per call, or a `waits-for` gate when the child count is dynamic:

```bash
bd dep add "$MERGE" "$EPIC.1"
bd dep add "$MERGE" "$EPIC.2"
bd dep add "$MERGE" "$EPIC.3"

# dynamic:
bd create "Summarize spawned work" --waits-for <spawner-id> \
  --waits-for-gate all-children      # or any-children
```

### Escalating to a human

```bash
bd create "Which auth provider should we standardize on?" -t decision -p 1 \
  -l human --description="Options and trade-offs: ..."
bd human list
bd human respond <id> -r "Use OAuth2 with our existing IdP"   # comments + closes
bd human dismiss <id> --reason "No longer applicable"
bd human stats
```

For a hard block, pair it with a `human` gate so downstream work actually
stops: `bd gate create --type=human --blocks <id> --reason "Needs a decision"`.

---

## 4. Merge slots — serializing conflict-prone work

Only one agent at a time may hold the slot. Use it for merge-queue conflict
resolution and anything else where concurrent agents would fight.

```bash
bd merge-slot create                 # once per project; ID is <prefix>-merge-slot
bd merge-slot check                  # available | held by <holder> | not found
bd merge-slot acquire [--holder me] [--wait]
bd merge-slot release [--holder me]
```

Mechanics: the slot is a bead where `status=open` means available,
`status=in_progress` means held, `metadata.holder` is the current holder, and
`metadata.waiters` is a priority-ordered queue. `--wait` enqueues instead of
failing. Always release in a `trap`/`finally` — a leaked slot stalls everyone.

---

## 5. Multi-repo routing

Routing decides which repository's database receives each new bead. It is
**opt-in**: with no routing configuration, everything lands in the current repo
and nothing here applies.

The problem it solves: you fork an OSS project that uses beads, and every
planning bead you create writes to the fork's `.beads/`, diverging from
upstream in every PR. Routing redirects `bd create` to a separate planning repo
that is never pushed upstream.

### Precedence

1. `--repo <path>` on the command — always wins
2. `routing.mode: auto` — route by detected role
3. `routing.default` — everything else (defaults to `.`)

Reads follow the same routing: with routing active, `bd list` and `bd ready`
read from the routed repository, and `bd show` falls back to it when a bead is
not found locally.

### Role detection

`git config beads.role` is the source of truth:

```bash
bd config set beads.role contributor    # stored in GIT config, not the database
bd config get beads.role
```

Unset, `bd` warns and falls back to a deprecated URL heuristic (fork pattern →
contributor; SSH or credentialed HTTPS origin → maintainer; plain HTTPS →
contributor; no remote → maintainer). SSH does not reliably indicate push
access, so set the role explicitly and the heuristic never runs.

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `routing.mode` | unset | `auto` routes by role; `explicit`/unset uses `routing.default` |
| `routing.default` | `.` | Target when auto is off |
| `routing.maintainer` | `.` | Target for maintainers in auto mode |
| `routing.contributor` | `~/.beads-planning` | Target for contributors in auto mode |
| `repos.primary` | unset | Primary repo for hydration |
| `repos.additional` | unset | Repos to hydrate from |
| `beads.role` | unset | `maintainer` \| `contributor` (git config) |

```bash
bd config show            # every source: config.yaml, database, git, env
bd config validate        # checks routing.mode and sync settings
bd where                  # which database this directory actually uses
```

### Setup

```bash
bd init --contributor     # wizard: creates the planning repo, sets routing.mode=auto,
                          # adds it to repos.additional, points sync at `upstream` on forks
bd init --team            # team wizard (usually no routing needed)
bd create "Private experiment" --repo ~/.beads-planning-personal
```

Plain `bd init` also detects the fork pattern (an `upstream` remote differing
from `origin`) and applies contributor configuration automatically; opt out with
`--role maintainer`.

---

## 6. Multi-repo hydration

Routing writes beads elsewhere, which means your database does not contain
them. **Hydration** imports beads from other repos into your database, each
tagged with its `source_repo`, giving one unified `bd list` / `bd ready`.

```bash
bd repo add ~/.beads-planning       # add a hydration source
bd repo list
bd repo sync [--verbose]            # import from all additional repos
bd repo remove ~/.beads-planning    # removes its hydrated beads too
```

`bd repo sync` reads each additional repo's `.beads/issues.jsonl` export and
imports the beads with their original prefixes and `source_repo` set, skipping
repos whose export has not changed (mtime cache). Note the dependency on the
JSONL export: hydration sources need `export.auto` enabled or a manual
`bd export` to stay fresh.

Once hydrated, they are ordinary rows:

```bash
bd list --json | jq '.[] | select(.source_repo == "~/.beads-planning")'
bd dep add impl-42 plan-10 --type blocks     # cross-repo edge
```

`bd doctor` warns when a routing target is missing from `repos.additional`
(the classic "my beads vanished" cause).

Moving beads between repos:

```bash
bd migrate issues --from ~/.beads-planning --to . --dry-run
bd migrate issues --from . --to ~/archive --id bd-abc --id bd-xyz --include closure
bd migrate issues --from ~/r1 --to ~/r2 --priority 1 --type bug --status open
```

`--include none|upstream|downstream|closure` controls how much of the
dependency neighbourhood travels with the selection.

---

## 7. One agent, many projects

Each project keeps its own isolated database and `bd` auto-discovers it by
walking up from the current directory, like git.

- Run **one** MCP server instance, not one per project — it resolves the
  workspace from each request's working directory. One instance per project
  invites operations landing in the wrong database.
- On a machine with many projects in server mode, use
  `bd init --shared-server` (or `BEADS_DOLT_SHARED_SERVER=1`) so all projects
  share one server at `~/.beads/shared-server/`. Every project must have a
  unique prefix — the prefix is the database name.
- `bd where` is the authoritative answer to "which workspace am I in".

---

## 8. Federation — peer-to-peer across teams

Federation syncs independent beads databases ("towns") directly with each
other over Dolt remotes. No central server; each town stays autonomous.

```bash
bd federation add-peer <name> <endpoint>
bd federation list-peers
bd federation sync [--peer <name>] [--strategy ours|theirs]
bd federation status [--peer <name>]
```

Peer names: start with a letter, alphanumeric plus `-`/`_`, ≤64 chars.

Endpoints: `dolthub://org/repo`, `gs://bucket/path`, `s3://bucket/path`,
`az://…`, `file:///path`, `https://host/path`, `ssh://host/path`,
`git@host:path`.

```bash
bd federation add-peer staging dolthub://myorg/staging-beads
bd federation add-peer backup  gs://mybucket/beads-backup
bd federation add-peer partner dolthub://partner-org/beads
bd federation add-peer town-gamma 192.168.1.100:3306/beads --user sync-bot
```

Credentials given with `--user`/`--password` are stored AES-256 encrypted
locally and used automatically. Connectivity is validated on first push/pull,
not at `add-peer`, so you can configure ahead of infrastructure.

Without `--strategy`, a sync that hits merge conflicts **pauses** and reports
the conflicting tables rather than auto-resolving.

Sovereignty tiers (`federation.sovereignty`): `T1` full sovereignty (data never
leaves controlled infrastructure), `T2` regional, `T3` trusted provider, `T4`
unrestricted. Topologies: hub-spoke, mesh, hierarchical.

Wisps are excluded from federation push by default
(`federation.exclude_types: [wisp]`), so execution traces never enter shared
history. Federation commands require a CGO build and direct database access.

Against a Dolt SQL server, federation uses two ports: 3306 for SQL, 8080 for
remotesapi peer push/pull.

`bd federation push <peer>` / `pull <peer>` are not yet exposed;
`bd federation sync` covers the bidirectional case.

---

## 9. Bucket federation — two machines, one bucket

The BYO-cloud path: point both machines at one GCS/S3 path and you have a
federation with no server and no hosted account.

> The setup below works on 1.2.2. The **`bd sync` convenience loop** it
> references does not — substitute
> `bd dolt pull && bd recompute-blocked && bd dolt push` until it ships. See
> the callout under "The sync loop" below.

```bash
# Machine one (holds the database you want to share)
bd dolt remote add origin gs://my-bucket/beads/myproject
bd dolt push
bd dolt remote list

# Machine two
bd init --remote gs://my-bucket/beads/myproject     # clones, persists sync.remote
bd dolt remote list
bd list --status all --json | jq length             # parity check
```

Schemes: `gs://`, `s3://` (or Dolt's `aws://`), `az://`, `dolthub://`,
`https://`, `file://`, git SSH. **One bucket path per database** — two
databases sharing a path produce a divergence no merge can reconcile.

Use `bd dolt remote add`, never raw `dolt remote add`: bd registers through the
store API so a running server sees it immediately, and naming it `origin` also
persists `sync.remote` so `bd sync` needs no flags.

### The sync loop and its exit codes

> **Not in the 1.2.2 binary.** `bd sync` is registered on upstream `main` but
> 1.2.2 rejects it with `Error: unknown command`. Until it ships, drive the
> loop yourself: `bd dolt pull && bd recompute-blocked && bd dolt push`, and
> treat a non-zero exit from either Dolt step as "stop and look". The exit-code
> contract below applies to `bd sync` once available.

`bd sync` is pull → positive conflict check → repair `is_blocked` → push with
bounded retry.

```bash
bd sync
bd sync --remote mini
bd sync --json
# cron, every minute:
* * * * * cd /path/to/workspace && /usr/local/bin/bd sync --json >> /tmp/bd-sync.log 2>&1
```

| Exit | Meaning | Timer should |
|---|---|---|
| 0 | Synced, or nothing to do | nothing |
| 1 | Error (transport, auth, storage) | alert if it repeats |
| 2 | Merge conflict — halted, nothing pushed | **alert a human**; never auto-resolve |
| 3 | Retries exhausted (push race, another writer's dirty set) | nothing; next tick retries |
| 4 | Dirty working set is stuck, not busy | **alert a human**; no later tick will publish |

Choosing an interval:

- **Staleness *is* the interval** — every replica's view of the others is up to
  one full interval old, by construction.
- Lease TTL and reclaim grace must both exceed it (see §2).
- A longer interval means **more conflicts**, not just staler data:
  `updated_at` is touched by every mutation, so two replicas editing the same
  bead between syncs conflict even when the fields differ. Disjoint edits to
  *different* beads merge cleanly at any cadence.

Measured on a real two-machine deployment (~1.3k issues, ~115k chunks, GCS):
cold push 28s, clone 5s, incremental push ~4s, pull+merge ~1s. A 60-second
cadence is comfortable and mostly no-ops.

### Failure modes

| Symptom | Cause / fix |
|---|---|
| "remote does not exist" on push | Added with raw `dolt remote add`; re-register with `bd dolt remote add` |
| Pull fails on a fresh machine, no conflicts | No Dolt commit identity: `dolt config --global --add user.name/user.email` |
| Auth works in the shell but sync fails in server mode | `CALL DOLT_PUSH/PULL` runs inside the server process with its startup environment. bd routes cloud-credentialed remotes through a `dolt` CLI subprocess; if it still fails, restart the server with credentials in its environment |
| Remote not named `origin` on one machine | Pass `--remote` per machine, or rename so both match |
| `bd sync` exits 2 | Inspect `dolt_conflicts` in `.beads/dolt/<db>`, resolve with `dolt conflicts resolve --ours/--theirs`, commit, push, then let the timer resume |

### `bd sync` vs `bd federation sync`

| | `bd sync` | `bd federation sync` |
|---|---|---|
| Target | the workspace's configured remote | named peer towns |
| Conflicts | halts (exit 2), no override switch | `--strategy ours\|theirs` available |
| Use for | replicas of *one* database across machines | sharing across independent teams/orgs |

---

## 10. Coordination checklist

- Claim, do not assign, when agents self-select.
- One owner per bead at all times.
- Document every handoff in a comment; document progress in notes.
- Use labels (`needs-review`) for soft signals and `human` gates for hard stops.
- Serialize conflict-prone work with a merge slot.
- Sync at session end so other agents see your state (`bd dolt push`).
- Name replicas with `node_id` before relying on automated reclaim.
- Watch for stale claims: `bd list --status in_progress` plus `bd stale`.
