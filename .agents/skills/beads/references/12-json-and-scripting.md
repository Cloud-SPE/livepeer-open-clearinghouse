# 12 — JSON, scripting, and integration surfaces

How to drive beads programmatically without parsing human output.

---

## 1. The JSON contract

`--json` is the stable contract. `--format` is for human-readable variants.

### Current (legacy) shapes

**Object commands** (`create`, `ping`, …) emit an object with
`schema_version` alongside the data:

```json
{
  "schema_version": 1,
  "id": "beads-abc",
  "title": "Example issue",
  "status": "open",
  "priority": 1,
  "issue_type": "task",
  "created_at": "2026-04-20T12:00:00Z"
}
```

**List commands** (`list`, `ready`, `search`, `stale`, **and also `show`,
`close`, `update`** — one element per requested ID) emit a raw JSON array with
**no** top-level `schema_version`:

```json
[{"id":"beads-abc","title":"First"},{"id":"beads-def","title":"Second"}]
```

**Errors** emit `{"schema_version":1,"error":"…","code":"…","hint":"…"}` —
most on stderr, some command-result paths on stdout. `code` and `hint` are
optional. JSON-mode errors exit 1.

### The envelope (opt-in now, default in v2.0)

```bash
export BD_JSON_ENVELOPE=1
```

```json
{"schema_version": 1, "data": <original payload>}
```

The payload is untouched inside `.data` — objects, arrays, and maps all wrap
identically. A `--limit`-truncated listing (currently wired for `bd ready`)
also carries `pagination`:

```json
{"schema_version":1,"data":[…],"pagination":{"returned":10,"total":42,"truncated":true}}
```

`total` is omitted when unknown; the whole key is absent when nothing was
truncated. In legacy mode you get a stderr text hint instead.

Consumer migration:

```bash
bd list --json | jq '.[0].id'          # legacy
bd list --json | jq '.data[0].id'      # envelope
```

Without the env var, `bd` prints a deprecation notice to stderr — but only when
stderr is a TTY and at most once per invocation, so scripts never see it.

### Rules for consumers

1. Check `schema_version` on object output; if it is higher than expected, warn
   but still try to parse — additive changes do not bump it.
2. Parse list commands as arrays.
3. **Ignore unknown fields.** New fields appear without a version bump.
4. Use `--json`, never `--format json`.

`schema_version` increments only when fields are added/renamed/removed,
structure changes, or types change.

---

## 2. Field contracts per command

**`bd list --json`** — required per item: `id`, `title`, `status`, `priority`,
`issue_type`, `created_at`. Optional: `description`, `owner`, `updated_at`,
`closed_at`, `labels[]`, `dependencies[]`, `dependency_count`,
`dependent_count`, `comment_count`, `parent`.

**`bd ready --json`** — same schema, filtered to unblocked beads.

**`bd blocked --json`** — standard fields plus `blocked_by_count` and
`blocked_by[]`.

**`bd show --json`** — an array, one element per requested ID, no
`schema_version` (pinned by a contract test). Adds `description`,
`acceptance_criteria`, `revision` (guarded-write concurrency token, always
present, legacy rows carry `0`), `dependencies[]`, and `comments[]` **only**
with `--include-comments`. Without it you get `comment_count`, plus
`comments_omitted: true` when a nonzero count was left out.

**`bd import --json`** — `source`, `created`, `updated`, `unchanged`,
`skipped`, `dedup_skipped`, `memories`, `ids[]`, `updated_issues[]`,
`tie_kept_local_ids[]`, `stale_skipped_ids[]`, `skipped_dependencies[]`,
`dry_run`.

**`bd export --json`** — JSONL, one self-contained record per line,
discriminated by `_type` (`"issue"` / `"memory"`), **not** wrapped in an
envelope and carrying no `schema_version`. The interchange's own version marker
is an optional `{"_schema":"beads-jsonl/1"}` header record that readers skip.
Issue records may carry `wisp_plane` — the explicit wisps-plane marker used to
route the storage plane on import (never routed by `no_history`); its absence
means the durable issues table. The v0.35–v0.37 `wisp` key is honored as a
read-side alias for `ephemeral`.

---

## 3. Everyday jq recipes

```bash
# IDs only
bd ready --json | jq -r '.[].id'

# Highest-priority ready bead
bd ready --json | jq -r 'sort_by(.priority) | .[0].id'

# Ready beads carrying a label
bd ready --json | jq '.[] | select(.labels[]? == "backend")'

# Title + status table
bd list --json | jq -r '.[] | "\(.id)\t\(.status)\t\(.title)"'

# What is blocking what
bd blocked --json | jq -r '.[] | "\(.id) ← \(.blocked_by | join(", "))"'

# Execution hints before delegating to a subagent
bd show <id> --json | jq '.[0] | {id,title,metadata,description,notes}'

# Everything closed this week
bd list --all --closed-after "$(date -d '7 days ago' +%F)" --json | jq length

# Config value
JIRA_URL=$(bd config get --json jira.url | jq -r '.value')
bd config list --json | jq -r '.["jira.project"]'
```

---

## 4. Bulk operations

### `bd batch` — one transaction, one Dolt commit

Designed for scripts that would otherwise invoke `bd` in a loop (severe write
amplification on server-backed stores). All operations run in a single Dolt
transaction: any error rolls back the whole batch.

```
close <id> [reason...]
update <id> <key>=<value> [<key>=<value> ...]      # status, priority, title, assignee
create <type> <priority> <title...>
dep add <from-id> <to-id> [type]
dep remove <from-id> <to-id>
# comments and blank lines are ignored
```

```bash
printf 'close bd-1 done\nupdate bd-2 status=in_progress\n' | bd batch
bd batch -f operations.txt -m "Sprint cleanup"
bd batch --dry-run -f operations.txt
bd list --status stale -q | awk '{print "close",$1," stale"}' | bd batch
```

Tokens are whitespace-separated; double-quoted strings may contain spaces
(`\"` and `\\` escape). This is a deliberately narrow subset — `show`, `list`,
`ready`, and flag-bearing creates are **not** accepted.

### Other bulk paths

```bash
# Create a whole plan (beads + edges) from one JSON file
bd create --graph plan.json

# Create several beads from markdown
bd create -f issues.md

# Bulk dependency wiring
bd dep add --file deps.jsonl          # {"from":"<dependent>","to":"<blocker>","type":"blocks"}

# Classic xargs loops (fine for tens, not thousands)
bd list --status open --priority 4 --json | jq -r '.[].id' | xargs -I{} bd update {} --priority 3
bd list --label sprint-1 --status open --json | jq -r '.[].id' | xargs -I{} bd close {} --reason "Sprint complete"
```

For very large batches, prefer `bd batch` or `--dolt-auto-commit batch` plus a
single `bd dolt commit`.

### Markdown import format

```markdown
# Fix Authentication Bug

### Type
bug

### Priority
1

### Labels
auth, backend, urgent, needs-review

### Description
Users can't log in after the recent deployment.
```

```bash
bd create -f issue.md
```

---

## 5. `bd query` — the query language

```bash
bd query "status=open AND priority<=2 AND updated>7d"
bd query "(status=open OR status=blocked) AND priority<2"
bd query "type=bug AND label=urgent"
bd query "assignee=none AND type=task"
bd query "NOT status=closed"
bd query "created>30d AND status!=closed"
bd query "id=bd-*"
bd query "status=open" --parse-only        # show the AST
```

Operators `= != > >= < <=`; boolean `AND OR NOT` (case-insensitive) with
parentheses. Fields: `status`, `priority`, `type`, `assignee`, `owner`,
`label`, `title`, `description`, `notes`, `created`, `updated`, `started`,
`closed`, `id`, `spec`, `pinned`, `ephemeral`, `template`, `parent`,
`mol_type`. `none` means empty. Dates: `7d`, `24h`, `2w`, `2026-01-15`,
`2026-01-15T10:00:00Z`, `tomorrow`, `"next monday"`, `"in 3 days"`.

Flags: `-a/--all` (include closed), `-n/--limit` (default 50, 0 = unlimited),
`--sort`, `-r/--reverse`, `--long`, `--offset` (proxied-server only).

**Gotcha:** dependency-blocked beads keep `status=open`, so
`bd query "status=blocked"` will not find them. Use `bd blocked`.

---

## 6. `bd sql` — raw SQL

**Server mode only.** Not available against the default embedded database.

```bash
bd sql 'SELECT COUNT(*) FROM issues'
bd sql 'SELECT id, title FROM issues WHERE status = "open" LIMIT 5'
bd sql --csv 'SELECT id, title, status FROM issues'
bd sql --json 'SELECT ...'
bd sql "SELECT event_type, actor, created_at FROM events WHERE issue_id = 'bd-a1b2' ORDER BY created_at DESC LIMIT 20"
```

SELECTs return a table (or JSON/CSV); non-SELECTs report rows affected.

Direct DML **bypasses the storage layer**: it is not journaled, not validated,
and can desynchronize derived state such as `is_blocked`. Treat it as a
read-only debugging tool. The `internal/` Go packages are not a public API —
the CLI is the supported integration boundary.

---

## 7. Events, hooks, and the journal

Three different things are called "events". Pick deliberately.

| System | What | Reach for it when |
|---|---|---|
| **Script hooks** | Executable `on_create` / `on_update` / `on_close` scripts in `.beads/hooks/`, fire-and-forget: async, output discarded, failures neither block nor retry | A side effect is nice to have (chat ping, cache bust) and losing one occasionally is fine |
| **Audit history** | The per-bead trail behind `bd history <id> --events` | A person asks who changed what, when |
| **Events journal** | One workspace-wide, sequence-ordered stream of committed mutations, replayable from a checkpoint | A machine keeps its own copy of the graph in step |

Stored event kinds include `issue.created`, `issue.updated`, `issue.closed`,
`dependency.added`, `sync.completed`.

### The events journal

> **Not in the 1.2.2 binary.** `bd events` (and the `bd serve` HTTP surface
> below) are registered on upstream `main` but 1.2.2 rejects them with
> `Error: unknown command`, and the `events-journal*` config keys go with them.
> Everything below documents the design as upstream describes it, for when it
> ships. On 1.2.2, the available substitutes are `bd history <id>` for
> per-bead audit trails and polling `bd export` / `bd list --json` for an
> external mirror.

Off by default, local to one clone, bounded once on.

```bash
bd config set events-journal true       # per workspace (.beads/config.yaml)
BD_EVENTS_JOURNAL=1 bd close bd-a1b2    # per process
```

| Key | Default | Effect |
|---|---|---|
| `events-journal` | false | Master switch; on costs one snapshot write per mutation |
| `events-journal-retain-days` | 7 | Keep everything younger than this; 0 disables the floor |
| `events-journal-retain-rows` | 100000 | Always keep this many newest records; 0 disables |
| `events-journal-auto-prune` | true | Enforce the floors automatically |

Enabling does **not** backfill: baseline a new consumer from an export or a
full read, then follow the journal.

Reading:

```bash
bd events tail --since 0                  # everything retained, oldest first
bd events tail --since 4211               # resume from a checkpoint
bd events tail --since 4211 --follow      # keep printing (polls ~1/s)
bd events tail --since 4211 --limit 100
bd events export                          # whole journal, same as --since 0
bd events prune --before 4000
```

JSON Lines, one record per line, in sequence order:

```json
{"seq":1,"ts":"2026-01-02T03:04:05Z","op":"create","issue_id":"bd-100","issue":{…}}
{"seq":11,"ts":"2026-01-02T03:04:05Z","op":"delete","issue_id":"bd-100","issue":null}
```

Record contract: `seq` (assigned inside the mutation's transaction; gapless,
strictly increasing, never reused — a rolled-back write burns no number), `ts`,
`op`, `issue_id`, `issue` (full post-mutation state, `null` on delete), plus
`dep` on `dep_add`/`dep_remove` and `comment` on `comment`.

Public operation vocabulary: `create`, `update` (any field/label/metadata/claim
/lease/promote/defer-wake/`is_blocked` flip), `close` (a reopen is an
`update`), `delete`, `dep_add`, `dep_remove`. A seventh, `comment`, is
journaled with `comment.source` ∈ {`structured`, `audit`}; a public projection
should **skip** comment records rather than fault on them. An ID rename has no
op of its own — it replays as remove-edges, delete, create, re-add.

`issue` is the row plus labels plus `is_blocked` (so a dependency change
replays without recomputing the graph); it never inlines dependencies or
comments.

Dependency records are asymmetric: a `dep_add` is recorded for **every**
accepted add including an idempotent metadata-refreshing re-add (treat it as an
upsert, never as proof the edge is new), while a `dep_remove` naming an
already-absent edge records nothing. `dep.metadata` is the caller's value on
add and the stored column read back on remove — compare parsed values, not
strings.

### Truncation

If `--since` falls below the oldest retained record, the read **fails** rather
than silently skipping:

```json
{"code":"events_journal_truncated",
 "error":"events journal truncated: checkpoint 12 is below the retained window [41..980]; records 13..40 were pruned",
 "floor":41,"head":980,"since":12,"schema_version":1}
```

Two ways forward, and bd takes neither for you: **accept the gap** (resume from
`floor - 1`) or **re-baseline** (rebuild from current state, then follow from
`head`). Re-reading records is harmless — each carries a full snapshot, so
applying one twice equals applying it once.

A hole in the *middle* of the window (only possible from a restored or
hand-edited journal) reports `since` as the last contiguously servable
sequence. There the best move is a third one: drain the intact stretch first
with `--limit (response.since - your since)`, then take the gap.

### Over HTTP

A consumer already talking to `bd serve` reads the same journal:

```bash
curl 'http://127.0.0.1:8080/v0/beads/events?since=4211&limit=500'
curl -N 'http://127.0.0.1:8080/v0/beads/events:watch?since=4211'   # SSE
```

- `since` is **required**; missing/negative is a 400, never "from the start".
- `limit` 1–10000, default 1000; `limit=0` is refused here.
- `head` is the highest sequence ever assigned — you are caught up when the
  last record's `seq` equals `head`. A full page proves nothing.
- 410 `events_journal_truncated` carries the same window; 409
  `events_journal_disabled` means this workspace's journal is off (the
  capability is always advertised, so treat 409 as workspace state).
- SSE: each `data` is one record, `id` is its `seq`; comment lines are 20s
  heartbeats. Reconnect with `Last-Event-ID`, which **overrides** `since`. At
  most 48 concurrent streams before 503 `events_watch_saturated` — poll unless
  the latency is the point.
- Delivery is at-least-once against your own checkpoint: no gaps within a
  stream, duplicates only across a reconnect. Be idempotent on `seq` and
  advance your checkpoint only after your own write lands.
- A `truncated` **event** can arrive mid-stream when a prune overtakes you;
  stop and re-baseline. Ignoring it stalls the consumer loudly (repeated
  connect-time 410s at a 60s retry).

**Publishing the journal publishes history, not current state** — every
retained record carries a full snapshot including titles since edited and beads
since deleted, and an HTTP reader bypasses the filesystem permissions
`bd events tail` requires. Weigh that before binding with
`--allow-non-loopback` (which requires `--auth-token-file`, and that token is
shared and surface-wide).

### What the journal does **not** cover

- **One replica, one sequence space.** Checkpoints are meaningless across
  replicas; a fresh clone restarts at 1. Dropping/restoring the journal tables
  also resets `seq` to 1, which makes a parked consumer read as "caught up"
  forever with no error. Re-baseline after either.
- **One branch.** Records arrive by direct write, not by merge.
- **Sync is not journaled.** `bd dolt pull` arrives as data; re-baseline a
  mirror after a sync.
- **Raw SQL is not journaled.**
- **Store-open writes** (schema migrations, version reconciliation) are not.
- **Compaction and `bd restore --apply` are not** — a mirror of a compacted
  bead stays stale until that bead's next journaled mutation.

Storage: `bd_events_journal` and `bd_events_seq`, both in `dolt_ignore`.
Working-set state: never versioned, staged, pushed, pulled, or federated — and,
like untracked files in git, they survive `dolt reset --hard`. That locality is
what buys the gapless per-clone sequence. Pruning frees rows, not disk; pair it
with `dolt gc`.

Retention passes run after mutating commands and on a timer inside `bd serve`,
capped at a few batches per invocation and throttled to roughly one pass per
hour per workspace. A pass maintains **the workspace whose command triggered
it** — a workspace only ever written remotely needs its own schedule.

> The floors are a time/count window, **not a consumer watermark.** A consumer
> further behind than both floors will be pruned past and lose records. Track
> your own watermark and size the floors for the longest outage you intend to
> survive.

---

## 8. Audit log

Separate from the journal: an append-only JSONL file at
`.beads/interactions.jsonl`, intended to be versioned in git, for answering
"why did the agent do that?" and for building SFT/RL datasets.

```bash
bd audit record --kind llm_call --model <m> --prompt "..." --response "..." --issue-id bd-42
bd audit record --kind tool_call --tool-name bash --exit-code 0 --issue-id bd-42
bd audit record --stdin < entry.json
bd audit label <entry-id> --label good --reason "correct fix, minimal diff"
```

Entries are append-only; labelling creates a new entry referencing the parent.

---

## 9. Observability (OpenTelemetry)

Disabled by default, zero overhead when unset — `telemetry.Init()` installs
no-op providers.

```bash
export BD_OTEL_ENABLED=true          # MASTER SWITCH; OTEL_* alone does nothing
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://localhost:8428/opentelemetry/api/v1/push

# local debugging
BD_OTEL_ENABLED=true OTEL_TRACES_EXPORTER=console OTEL_METRICS_EXPORTER=console bd list
```

`bd` deliberately does not auto-activate from a machine-global `OTEL_*` setting
meant for another tool. `OTEL_SDK_DISABLED=true` forces it off.
`OTEL_SERVICE_NAME` (default `bd`) and `OTEL_RESOURCE_ATTRIBUTES` shape the
resource. Log export is not implemented yet.

Metrics: `bd_storage_operations_total`, `bd_storage_operation_duration_ms`,
`bd_storage_errors_total` (attribute `db.operation`); `bd_db_retry_count_total`,
`bd_db_lock_wait_ms`; `bd_issue_count` (gauge by `status`);
`bd_ai_input_tokens_total`, `bd_ai_output_tokens_total`,
`bd_ai_request_duration_ms`.

Spans (exported only with `OTEL_TRACES_EXPORTER=console` in the recommended
stack): `bd.command.<name>` (attributes `bd.command`, `bd.version`, `bd.args`,
`bd.actor`), `dolt.exec|query|query_row`, `dolt.commit|push|pull|merge`,
`ephemeral.count|nuke`, `hook.exec` (with truncated `hook.stdout`/`hook.stderr`
events), `tracker.sync|pull|push`, `anthropic.messages.new`.

Legacy `BD_OTEL_METRICS_URL` / `BD_OTEL_LOGS_URL` / `BD_OTEL_STDOUT` still work,
activate telemetry on their own, win over a pre-existing `OTEL_*` value, and
log a one-line deprecation warning per invocation.

`bd metrics [on|off|example]` is unrelated: it controls anonymous **usage**
metrics (command name, bd version, OS platform only — never issues, paths,
remotes, identity, or user text). That is a user preference, not an agent
decision.

---

## 10. Key–value store

A small persistent scratchpad for flags and script state:

```bash
bd kv set feature_flag true
bd kv get feature_flag
bd kv list --json
bd kv clear feature_flag
```

Use it for automation state, not for tracked work — work belongs in beads.

---

## 11. Scripting checklist

- `--json` for every read you parse; never grep human output.
- Capture IDs with `bd q` or `bd create --silent` / `--json`.
- `bd batch` (or `--dolt-auto-commit batch` + `bd dolt commit`) for many writes.
- `BD_NON_INTERACTIVE=1` in CI (also implied by no TTY or `CI=true`).
- `--dry-run` exists on nearly every destructive command — use it first.
- Check exit codes: JSON-mode errors exit 1, and `bd config drift` exits 1 on
  drift. (`bd sync`'s richer 0–4 contract lands with `bd sync` itself, which is
  not in 1.2.2.)
- Embedded mode needs no server, which makes it the right choice in CI.
- Pin behaviour to a version: `bd version`, and read `bd info --whats-new`
  after upgrades.
