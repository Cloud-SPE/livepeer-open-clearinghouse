# 02 — Core concepts: beads, fields, IDs, labels, metadata

The data model. `03-dependencies-and-ready.md` covers the edges between beads;
this file covers the beads themselves.

---

## 1. What a bead is

A **bead** is one tracked unit of work. "Bead" and "issue" are the same thing —
the CLI says issue, the product says bead. Every bead has:

- a **hash ID** (`bd-a1b2`) that no other agent or branch can collide with,
- a **title** and (you should always write one) a **description**,
- a **type** (`task`, `bug`, `feature`, `epic`, …),
- a **priority** 0–4,
- a **status** moving `open` → `in_progress` → `closed`,
- typed **dependencies** to other beads,
- optional **labels**, **comments**, **notes**, and arbitrary **metadata**.

The whole product in one loop: creating and closing beads reshapes the graph,
and the graph — not a human dispatcher — decides what is workable next.

---

## 2. Full field reference

These are the fields stored in Dolt and emitted by `bd export`. Optional fields
are omitted when empty.

| Field | Type | Notes |
|---|---|---|
| `id` | string | Hash ID, e.g. `bd-a1b2`; hierarchical children get `bd-a1b2.1` |
| `title` | string | Required |
| `description` | string | The "why and what". Write it. |
| `design` | string | Design notes / chosen approach |
| `acceptance_criteria` | string | What "done" means; `bd lint` checks for it |
| `notes` | string | Running log; append with `bd note` |
| `status` | string | `open`, `in_progress`, `blocked`, `deferred`, `closed`, `pinned`, `hooked`, plus any `status.custom` |
| `priority` | int | 0 critical … 4 backlog; default 2 |
| `issue_type` | string | see §3; default `task` |
| `assignee` | string | Free-text agent/user name; there is no agent registry |
| `owner` | string | Set from identity at create time |
| `estimated_minutes` | int | `-e/--estimate` |
| `created_at` / `updated_at` | RFC3339 | `updated_at` is touched by *every* mutation |
| `created_by` | string | Actor (see `09-configuration.md` for resolution order) |
| `closed_at` / `close_reason` | RFC3339 / string | Set on close |
| `external_ref` | string | `gh-9`, `jira-ABC`, a Linear URL, … |
| `metadata` | JSON | Arbitrary extension data; see §8 |
| `labels` | []string | See §7 |
| `dependencies` | []Dependency | See `03-dependencies-and-ready.md` |
| `comments` | []Comment | Only streamed with `--include-comments` |
| `revision` | int | Optimistic-concurrency token, always present in `bd show --json` |

Workflow-layer field groups also exist: scheduling (`due_at`, `defer_until`),
claim leasing (`lease_expires_at`, `heartbeat_at`), gates (`await_type`,
`await_id`, `timeout`), and molecule/wisp fields (`ephemeral`, `mol_type`,
`bonded_from`). Internal fields `content_hash`, `source_repo`, and `id_prefix`
never appear in exports.

The schema is deliberately stable: put integration-, orchestrator-, or
team-specific data in `metadata` rather than asking for new first-class fields.

---

## 3. Issue types

`bd types` prints the live list. On 1.2.2 the built-ins are:

| Type | Use |
|---|---|
| `task` | General work item (default) |
| `bug` | Something broken |
| `feature` | New functionality |
| `chore` | Maintenance, tooling, dependencies |
| `epic` | Large body of work spanning multiple beads |
| `decision` | Architecture decision record (aliases `dec`, `adr`) |
| `spike` | Timeboxed investigation to reduce uncertainty |
| `story` | User story |
| `milestone` | Marks completion of a set; contains no work itself |

Aliases accepted by `--type`: `enhancement`/`feat` → `feature`, `dec`/`adr` →
`decision`, `mr` → `merge-request`, `mol` → `molecule` (filters).

Additional types (`agent`, `role`, `event`, `message`, …) require
`bd config set types.custom "agent,role,event"`. `types.infra` routes named
types into the ephemeral wisps table instead of the versioned issues table.

Some types are structural rather than work: `gate` (async wait — see
`05-workflows.md`), `molecule`, `message`. `bd list` hides gates and infra
beads unless you pass `--include-gates` / `--include-infra`.

---

## 4. Priorities

Numeric only. `-p 1` or `-p P1`; never "high".

| P | Level | Typical |
|---|---|---|
| 0 | Critical | Security, data loss, broken build, production down |
| 1 | High | Major feature, important bug |
| 2 | Medium (default) | Normal work |
| 3 | Low | Polish, optimization |
| 4 | Backlog | Future ideas |

Filters: `-p/--priority`, `--priority-min`, `--priority-max`. The project
roadmap idiom is `bd list --priority-max 1 --json` (everything P0–P1).

---

## 5. Statuses

`bd statuses` prints them with categories. Categories decide behaviour:

| Status | Category | In `bd ready`? | In default `bd list`? |
|---|---|---|---|
| `open` | active | yes | yes |
| `in_progress` | wip | no | yes |
| `blocked` | wip | no | yes |
| `deferred` | frozen | no | yes (with `--deferred` to isolate) |
| `closed` | done | no | no |
| `pinned` | frozen | no | no by default (`--pinned` / `--no-pinned`) |
| `hooked` | wip | no | yes |

Custom statuses supplement the built-ins and carry an optional category:

```bash
bd config set status.custom "in_review:active,qa_testing:wip,on_hold:frozen,archived:done"
```

A custom status with no category is valid but excluded from `bd ready`.

Important: a bead blocked by an open dependency keeps `status=open` — the
dependency-blocked state is *computed*, not stored. `bd query "status=blocked"`
will not find it; `bd blocked` will. The denormalized `is_blocked` flag backs
`bd ready`; repair it with `bd recompute-blocked` if a pull went sideways.

Transitions:

```bash
bd update <id> --claim              # → in_progress, assignee = you (atomic)
bd update <id> --status in_progress
bd defer <id> --until "next monday" --reason "waiting on API access"
bd undefer <id>
bd close <id> --reason "..."
bd reopen <id> --reason "regressed"
```

---

## 6. IDs

### Hash IDs

IDs are content-derived hashes (title, description, creator, creation time,
plus a collision nonce) — not sequence numbers. Two agents or two branches
creating beads simultaneously cannot mint the same ID, so merges never
renumber work.

Length adapts to database size to hold the birthday-paradox collision
probability under a threshold:

| Config key | Default | Meaning |
|---|---|---|
| `min_hash_length` | 3 | Floor for new IDs |
| `max_hash_length` | 8 | Ceiling |
| `max_collision_prob` | 0.25 | Target collision probability |

On collision, generation retries at base length, +1, +2 with ten nonces each.
Actively pruning closed beads keeps IDs short over time.

> Upstream's `core-concepts/hash-ids.md` says the minimum is 4; the shipping
> 1.2.2 binary and the configuration reference both use 3. Trust the binary.

### Hierarchical (child) IDs

`--parent` produces `parent.1`, `parent.2`, … up to `hierarchy.max-depth`
(default 3 levels).

```bash
EPIC=$(bd q "Auth System" -t epic -p 1)   # bd-a3f8e9
bd create "Design login UI"   -p 1 --parent "$EPIC"   # bd-a3f8e9.1
bd create "Backend validation" -p 1 --parent "$EPIC"  # bd-a3f8e9.2
bd children "$EPIC"                                    # includes closed
bd dep tree "$EPIC"
```

Children **inherit the parent's labels** by default. If the epic carries a
size/effort label, every child inherits it and `bd list -l large` returns the
whole tree. Pass `--no-inherit-labels` when the child needs its own estimate.

Importing children whose parent is missing is accepted rather than failing —
recreate the parent or close the orphans afterwards. Prevent it with
`bd delete --cascade` and by checking `bd children <id>` first.

### Sequential counter IDs (opt-in)

```bash
bd config set issue_id_mode counter   # bd-1, bd-2, ...
bd config set issue_id_mode hash      # back to default
```

Per-prefix counters, seeded from the highest existing numeric ID, atomic within
a Dolt session. Human-friendly, but counters diverge across parallel branches —
prefer hash IDs for multi-agent work. `--id` bypasses generation entirely and
does not advance the counter. Counter mode does not apply to wisps.

### Working with IDs

```bash
bd show a1b2          # partial ID match
bd search "bd-5q"     # fast prefix match on ID-like queries
bd rename bd-w382l bd-dolt          # rewrites every reference
bd rename-prefix kw- --dry-run      # rename the prefix for the whole DB
bd rename-prefix mtg- --repair      # consolidate a multi-prefix (corrupt) DB
```

---

## 7. Labels

Structured fields carry workflow state (status/priority/type). Labels carry
everything else: component, domain, effort, quality gates, ownership, release.

```bash
bd create "Fix auth bug" -t bug -p 1 -l auth,backend,urgent
bd label add bd-42 security,breaking-change     # comma-separated, no spaces
bd label remove bd-42 urgent
bd label list bd-42
bd label list-all --json                        # every label + usage count
bd label propagate <parent-id> <label>          # push a label to direct children
bd tag bd-42 needs-review                       # shorthand for a single add
```

Filtering:

```bash
bd list --label backend,auth        # AND — must have all
bd list --label-any frontend,ui     # OR  — must have at least one
bd list --label backend --label-any urgent,release-blocker   # combine
bd list --exclude-label wontfix
bd list --label-pattern 'tech-*'
bd list --label-regex 'tech-(debt|legacy)'
bd list --no-labels
```

Labels are **case-sensitive** (`Backend` ≠ `backend`) and are not shown in
human `bd list` output — use `--json`, `bd show`, or `bd label list`.

### Conventions worth adopting

- lowercase, hyphen-separated: `good-first-issue`
- component: `backend`, `frontend`, `api`, `database`, `cli`, `infra`
- domain: `auth`, `payments`, `search`, `billing`
- effort: `small`, `medium`, `large`
- quality gates: `needs-review`, `needs-tests`, `needs-docs`, `breaking-change`
- release: `v1.0`, `release-blocker`, `backport-candidate`
- process: `auto-generated`, `technical-debt`, `help-wanted`, `needs-triage`
- agent hygiene: `ai-generated`, `needs-human-review`

Keep the vocabulary small (roughly 5–10 technical + 3–5 domain + standard
process labels). Labels are for filtering, not free-text search.

### Labels as a state cache

For orchestration, labels can cache the *current* value of an operational
dimension using `<dimension>:<value>`, with an event bead as the immutable
source of truth.

```bash
bd set-state agent-abc patrol=muted --reason "Investigating stuck worker"
bd state agent-abc patrol        # → muted
bd state list agent-abc          # every dimension:value on the bead
bd list --label patrol:muted     # O(1) query instead of scanning events
```

`bd set-state` atomically creates the event bead, removes the old
`dimension:*` label, and adds the new one. Common dimensions: `patrol`
(active/muted/suspended), `mode` (normal/degraded/maintenance), `status`
(idle/working/blocked), `health` (healthy/warning/failing), `sync`
(current/stale/syncing). Keep dimensions orthogonal and value sets small; if
labels get corrupted, rebuild them from the events.

---

## 8. Metadata

`metadata` accepts arbitrary JSON and is the **preferred extension point** —
reach for it before proposing new fields.

```bash
bd create "Task" --metadata '{"team":"platform"}'
bd create "Task" --metadata @plan.json
bd update <id> --set-metadata team=platform --set-metadata sprint=12
bd update <id> --unset-metadata sprint
bd list --metadata-field team=platform
bd list --has-metadata-key execution_agent_type
bd show <id> --json | jq '.[0] | {id,title,metadata,description,notes}'
```

Reserved key prefixes: `bd:` (beads internal) and `_` (private). Prefer
namespaced keys for integrations (`{"example_tracker": {...}}`).

### Agent execution hints (a convention, not a schema)

| Key | Meaning |
|---|---|
| `execution_agent_type` | Worker class: `explorer`, `worker`, `mixed` |
| `execution_suggested_model` | Capability-tier suggestion, not a runtime binding |
| `execution_reasoning_effort` | `low`, `medium`, `high`, `xhigh` |
| `execution_mode` | local, delegated, or staged |
| `execution_parallel_group` | Grouping hint for parallelizable work |

An orchestrator must read these **before** spawning a subagent — model and
effort are fixed at launch, so reading them afterwards is too late. When
present, they outrank free-form notes for routing decisions. There is
deliberately no `bd show --execution` helper; the jq snippet above is the
supported access path.

---

## 9. Non-blocking graph links

Beyond dependencies, beads supports knowledge-graph edges. None of these affect
`bd ready`.

| Link | Created by | Effect |
|---|---|---|
| `relates-to` | `bd dep relate <a> <b>` | Bidirectional "see also"; remove with `bd dep unrelate` |
| `duplicates` | `bd duplicate <dup> --of <canonical>` | Closes the duplicate, records `duplicate_of` |
| `supersedes` | `bd supersede <old> --with <new>` | Closes the old bead, records `superseded_by` |
| `replies-to` | mail/orchestrator, or `bd dep add <new> <orig> --type replies-to` | Threading; view with `bd show <id> --thread` |

Bulk duplicate handling:

```bash
bd duplicates                 # exact-content groups + suggested merge target
bd duplicates --dry-run
bd duplicates --auto-merge    # re-parents children, closes dups, links them
bd find-duplicates --threshold 0.4          # fuzzy, token-similarity based
bd find-duplicates --method ai              # LLM comparison (needs ANTHROPIC_API_KEY)
```

`--auto-merge` picks the most-referenced bead in each group (ties → smallest
ID), only groups beads with matching status, re-parents children onto the
target, closes the duplicates with reason `Duplicate of <target>`, and links
each with a `related` edge.

---

## 10. Scheduling: due dates and deferral

```bash
bd create "Ship release" --due "+2w"
bd update <id> --due "next monday"     # empty string clears
bd defer <id> --until "+1d"            # hidden from bd ready until then
bd undefer <id>
bd list --overdue
bd list --due-before "+3d"
bd list --deferred
bd ready --include-deferred            # override the hide
```

Date formats: relative (`+6h`, `+1d`, `+2w`), natural language (`tomorrow`,
`next monday`, `in 3 days`), or absolute (`2026-01-15`, RFC3339).

---

## 11. Content lifecycle: compaction, restore, pruning

Closed beads accumulate. Three different tools:

| Tool | What it removes | Reversible? |
|---|---|---|
| `bd admin compact` | Replaces old closed beads' text with a summary | `bd restore <id> [--apply]` |
| `bd prune --older-than 30d --force` | Deletes closed **non-ephemeral** beads | no |
| `bd purge --force` | Deletes closed **ephemeral** beads (wisps) | no |

```bash
bd admin compact --stats
bd admin compact --analyze --json              # candidates with full content
bd admin compact --apply --id bd-42 --summary summary.txt
bd restore bd-42            # show the archived original
bd restore bd-42 --apply    # write it back
```

`bd prune` is reference-aware: it skips closed beads whose ID is mentioned in
the description, notes, or comments of any open bead — protecting ADRs and
decision beads that live work still cites. `--ignore-references` overrides.
`bd prune` requires `--older-than` or `--pattern` as a safety gate; use
`--pattern '*'` to really sweep everything closed.

Storage-level cleanup (`bd compact`, `bd flatten`, `bd gc`) is covered in
`08-sync-and-storage.md`.
