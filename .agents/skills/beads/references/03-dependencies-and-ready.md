# 03 — Dependencies, ready work, and the graph

The edges are the point of beads. Get the direction right and the tracker runs
itself; get it backwards and every agent stalls.

---

## 1. Direction: say "needs", never "before"

```bash
bd dep add <dependent> <blocker>      # "<dependent> NEEDS <blocker>"
```

`bd dep add bd-2 bd-1` means bd-2 depends on bd-1; bd-1 blocks bd-2; bd-2 will
not appear in `bd ready` until bd-1 closes.

Temporal phrasing inverts this and is the single most common agent error.
"Design comes before implementation" tempts `bd dep add design implement` —
backwards. Restate as a requirement: "implementation **needs** design" →
`bd dep add implement design`. Then verify:

```bash
bd blocked          # implementation should be listed, blocked by design
bd ready            # design should be listed, implementation should not
```

Equivalent spellings:

```bash
bd dep add bd-2 bd-1                    # positional
bd dep add bd-2 --blocked-by bd-1       # flag form
bd dep add bd-2 --depends-on bd-1       # alias, same meaning
bd dep bd-1 --blocks bd-2               # inverted shorthand: blocker first
bd link bd-2 bd-1                       # shorthand for dep add, --type blocks
```

---

## 2. Dependency types

`--type` on `bd dep add` / `bd link`. Default `blocks`.

**Blocking (affect `bd ready`):**

| Type | Semantics |
|---|---|
| `blocks` | Hard ordering; the blocker must close first |
| `parent-child` | Epic hierarchy; a blocked parent blocks its children (children are parallel to each other by default) |
| `conditional-blocks` | The dependent runs only if the other side **failed** — error-handling paths |
| `waits-for` | Waits for all (or any) of another bead's dynamically created children — fan-in |

**Non-blocking (graph annotations):**

| Type | Semantics |
|---|---|
| `discovered-from` | Provenance: found while working on that bead |
| `related` / `relates-to` | Informational link |
| `tracks` | Tracks another bead's progress |
| `caused-by` | Root-cause link |
| `validates` | Test or verification link |
| `supersedes` | Replaced by a newer bead (prefer `bd supersede`) |
| `until` | Time-scoped relation |
| `replies-to` | Message threading |

```bash
bd dep add bd-42 bd-41 --type tracks
bd create "Found race in auth" --deps discovered-from:bd-abc
bd create "Task" --deps "discovered-from:bd-20,blocks:bd-15"   # multiple at create time
```

`--deps` on `bd create` takes `type:id` pairs (or a bare `id`, meaning
`blocks`), comma-separated.

---

## 3. What `bd ready` actually computes

Ready = the claimable frontier. A bead is ready when **all** of these hold:

- its status is in an `active` category (`open`, or a custom `:active` status),
- it is not `in_progress`, `blocked`, `deferred`, `pinned`, or `hooked`,
- its `defer_until` is not in the future,
- every **blocking** dependency is closed,
- no open gate blocks it.

```bash
bd ready                       # tree view by default
bd ready --json                # the API surface
bd ready --plain               # plain numbered list
bd ready --explain             # why each bead is ready or blocked
bd ready --claim --json        # atomically claim the first match
```

`bd ready` is **not** `bd list --status open`. `list` shows open beads
regardless of blockers; `ready` runs the graph. (`bd list --ready` uses the same
blocker-aware semantics as `bd ready`.)

### Filters

```bash
bd ready --priority 1
bd ready --type task
bd ready --label backend            # AND across --label
bd ready --label-any api,cli        # OR
bd ready --exclude-label needs-triage
bd ready --exclude-type epic,convoy
bd ready --assignee agent-1
bd ready --unassigned
bd ready --parent bd-epic           # only descendants of an epic
bd ready --mol bd-molecule          # only steps inside a molecule
bd ready --gated                    # molecules whose gate just closed
bd ready --sort priority|hybrid|oldest
bd ready --limit 0                  # unlimited (default 100)
bd ready --include-deferred --include-ephemeral
bd ready --metadata-field team=platform
bd ready --has-metadata-key execution_agent_type
```

### `--explain` output

```
● Ready (1 issues):
  bd-1 [P1] Set up database
    Reason: no blocking dependencies
    Unblocks: 1 issue(s)

● Blocked (2 issues):
  bd-2 [P2] Create API
    ← blocked by bd-1: Set up database [open]
```

Machine-readable form:

```bash
bd ready --explain --json | jq '.blocked[0]'
# {"id":"bd-3","title":"Add authentication",
#  "blocked_by":[{"id":"bd-2","title":"Create API","status":"open"}]}
```

### "`bd ready` shows nothing but I have open issues"

Almost always real blockers. Diagnose in this order:

```bash
bd blocked                      # what is stuck and on what
bd dep tree <id>                # the chain above a specific bead
bd list --status in_progress    # already claimed, so not "ready"
bd list --deferred              # deferred out of the frontier
bd gate list                    # an open gate holding a step
bd recompute-blocked            # repair stale is_blocked flags after a bad pull
```

`bd recompute-blocked` is idempotent and works in both embedded and server mode.
Reach for it when a pull was interrupted or a conflict was resolved by hand:
`bd ready` trusts the denormalized flag, so a stale flag silently hides ready
work or surfaces blocked work.

---

## 4. Seeing what is blocked

```bash
bd blocked                      # every blocked bead + its blockers
bd blocked --parent bd-epic     # scoped to an epic's descendants
bd blocked --json               # adds blocked_by_count and blocked_by[]
```

Run it after every close — it is how you learn what you just unblocked.
`bd close <id> --suggest-next` prints the same information inline.

---

## 5. Inspecting and visualizing the graph

```bash
# Trees
bd dep tree <id>                       # what blocks this (default: down)
bd dep tree <id> --direction up        # what this blocks
bd dep tree <id> --direction both
bd dep tree <id> --status open
bd dep tree <id> --max-depth 3         # default 50
bd dep tree <id> --format mermaid
bd dep tree <id> --show-all-paths      # no dedup on diamond dependencies

# Flat lists
bd dep list <id>                       # dependencies (down)
bd dep list <id> --direction up        # dependents
bd dep list <id> --type tracks
bd dep list a b c --json               # batch; flat array across all IDs

# Graphs
bd graph <id>                          # terminal DAG (default)
bd graph --all                         # all open beads by connected component
bd graph --compact <id>                # one line per bead
bd graph --box <id>                    # ASCII boxes with layers
bd graph --dot <id> | dot -Tsvg > graph.svg
bd graph --html <id> > graph.html      # self-contained D3 view
bd graph check                         # integrity: exit 0 clean, 1 problems
```

Layer semantics: layer 0 has no dependencies and can start immediately; higher
layers depend on lower ones; **beads in the same layer can run in parallel**.
That is the parallelization plan for a fleet of agents.

Status icons: `○ open`, `◐ in_progress`, `● blocked`, `✓ closed`, `❄ deferred`.

---

## 6. Cycles

Beads rejects cycles at write time — `bd dep add` checks before committing.
`--no-cycle-check` skips the per-edge check for bulk wiring; a bulk
`--file` add still runs one whole-graph check before committing.

```bash
bd dep cycles          # detect
bd graph check         # detect as part of integrity checking
bd dep remove <dependent> <blocker>    # break the weakest link
bd blocked && bd ready                 # verify the cycle is gone
```

Prevention: think "X needs Y", not "X before Y"; keep chains shallow; run
`bd blocked` after wiring a batch of edges.

---

## 7. Bulk wiring

For a large plan, wire edges from JSONL instead of N invocations:

```bash
cat > deps.jsonl <<'EOF'
{"from":"bd-42","to":"bd-41"}
{"from":"bd-43","to":"bd-41","type":"blocks"}
EOF
bd dep add --file deps.jsonl
cat deps.jsonl | bd dep add --file -
```

`from` is the dependent, `to` is the blocker (aliases `issue_id` /
`depends_on_id` are accepted). To create a whole plan — beads *and* edges — in
one call, use `bd create --graph plan.json`, or `bd batch` for mixed write
operations in a single transaction (see `12-json-and-scripting.md`).

---

## 8. Cross-repository dependencies

Two distinct mechanisms.

**Depend on a specific bead in another hydrated repo** — ordinary edge, after
`bd repo add` / `bd repo sync` (see `07-multi-agent.md`):

```bash
bd dep add impl-42 plan-10 --type blocks
```

**Depend on a *capability* another project publishes** — resolved at query
time, always blocking:

```bash
# consumer
bd dep add bd-42 external:backend:api-ready

# producer, once the work is closed
bd ship api-ready              # adds provides:api-ready to the export: bead
bd ship api-ready --force      # even if the bead is not closed
bd ship api-ready --dry-run
```

`bd ship` finds the bead labelled `export:<capability>`, checks it is closed,
and adds `provides:<capability>`. The consumer resolves the target through the
`external_projects` config map:

```yaml
external_projects:
  backend: ../backend
  other-project: /absolute/path/to/other-project
```

---

## 9. Epics: hierarchy plus edges

`--parent` gives structure; `bd dep add` gives ordering. You almost always want
both.

```bash
EPIC=$(bd q "Payments v2" -t epic -p 1)
A=$(bd q "Design schema"     -p 1); bd update "$A" --parent "$EPIC"
B=$(bd q "Implement API"     -p 1); bd update "$B" --parent "$EPIC"
C=$(bd q "Integration tests" -p 1); bd update "$C" --parent "$EPIC"

bd dep add "$B" "$A"
bd dep add "$C" "$B"

bd dep tree "$EPIC"
bd ready --parent "$EPIC"
```

Epic bookkeeping:

```bash
bd children <epic>                 # includes closed children by default
bd children <epic> --pretty
bd epic status                     # completion status of every epic
bd epic status --eligible-only     # epics whose children are all closed
bd epic close-eligible --dry-run
bd epic close-eligible             # close them
bd swarm validate <epic>           # check the DAG is swarm-ready
```

**Closing the last child does not close the epic.** Epics stay open as
close-eligible work until something closes them; `bd epic close-eligible` is
the sweep. `bd mol stale` finds the same condition for molecules.

---

## 10. Fan-out / fan-in

```bash
# Fan-out: independent parts under one epic
bd create "Part A" --parent "$EPIC"
bd create "Part B" --parent "$EPIC"
bd create "Part C" --parent "$EPIC"

# Fan-in: a merge step that needs all three (one edge per call)
MERGE=$(bd q "Integrate parts" -p 1)
bd dep add "$MERGE" "$EPIC.1"
bd dep add "$MERGE" "$EPIC.2"
bd dep add "$MERGE" "$EPIC.3"
```

When the number of children is not known until runtime, use the `waits-for`
gate instead of enumerating edges:

```bash
bd create "Summarize spawned work" \
  --waits-for <spawner-id> --waits-for-gate all-children   # or any-children
```

In a formula, the same thing is `waits_for = "all-children"` /
`"any-children"` / `"children-of(step-id)"` — see `05-workflows.md`.
