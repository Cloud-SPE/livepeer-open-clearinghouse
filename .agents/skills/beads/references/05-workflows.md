# 05 — Workflows: formulas, molecules, wisps, gates, swarms

Repeatable multi-step work is declared once and stamped out on demand. This is
the layer above plain beads and edges — reach for it when the *same shape* of
work recurs (release checklists, review pipelines, per-worker patrols).

Most work never needs this. Epics + dependencies handle the ordinary case.

---

## 1. The pipeline and its vocabulary

```
formula (TOML/JSON file)
   │ bd cook
   ▼
proto  (template epic, {{variables}} intact, carries the `template` label)
   │ bd mol pour            │ bd mol wisp
   ▼                        ▼
molecule (persistent)      wisp (ephemeral)
```

The CLI uses a chemistry metaphor:

| Phase | Term | Command | Lifecycle |
|---|---|---|---|
| solid | **proto** | `bd cook` | reusable template, not live work |
| liquid | **molecule** | `bd mol pour` | persistent beads, synced like any bead |
| vapor | **wisp** | `bd mol wisp` | ephemeral; excluded from federation push; deleted by `bd purge` |

A **molecule is just an epic with execution intent**: a parent bead whose
children flow through `bd ready` as dependency-ordered steps. Any epic with
children is a molecule; protos and formulas are optional layers for reuse.

A **gate** is a bead representing an async wait — a human decision, a timer, a
GitHub run or PR — that blocks a step until the world catches up.

---

## 2. Formulas

Formulas live in the search path, first match wins:

1. `<resolved-beads-dir>/formulas/` (active project)
2. `<checkout-root>/.beads/formulas/` (repo-local)
3. `~/.beads/formulas/` (user)
4. `$GT_ROOT/.beads/formulas/` (shared workspace root, if set)

```bash
bd formula list [--type workflow|expansion|aspect|convoy]
bd formula show <name> [--json]     # what the parser ACTUALLY understood
bd formula convert <name> [--all] [--delete] [--stdout]   # JSON → TOML
bd mol seed <name> [--var k=v]      # verify a formula is reachable and cookable
```

TOML is preferred (multi-line strings, comments, readable diffs).

### Minimal formula

```toml
formula = "feature-workflow"
description = "Standard feature development workflow"
version = 1
type = "workflow"          # workflow | expansion | aspect

[vars.feature_name]
description = "Name of the feature"
required = true

[[steps]]
id = "design"
title = "Design {{feature_name}}"
description = "Write the design note"

[[steps]]
id = "implement"
title = "Implement {{feature_name}}"
needs = ["design"]

[[steps]]
id = "review"
title = "Code review"
needs = ["implement"]

[[steps]]
id = "merge"
title = "Merge to main"
needs = ["review"]
```

### Variables

```toml
[vars.version]
description = "Release version"
required = true
pattern = "^\\d+\\.\\d+\\.\\d+$"

[vars.environment]
description = "Target environment"
default = "staging"
enum = ["staging", "production"]
```

Use them anywhere a string is accepted: `title = "Deploy {{version}} to {{environment}}"`.

### Step fields

- `id` — referenced by `needs` and by bond points
- `title`, `description` — support `{{vars}}`
- `type` — sets the created bead's issue type: `task` (default), `bug`,
  `feature`, `epic`, `chore`. **Anything else silently falls back to `task`.**
  Human sign-offs and async waits are `[steps.gate]` blocks, not step types.
- `needs = ["a","b"]` — fan-in on named steps (this is a dependency, not a gate)
- `waits_for = "all-children" | "any-children" | "children-of(step-id)"` —
  wait for dynamically created children
- `[steps.gate]` — see §5

### Parallel then join

```toml
[[steps]]
id = "test-unit"
title = "Unit tests"

[[steps]]
id = "test-integration"
title = "Integration tests"

[[steps]]
id = "deploy"
title = "Deploy"
needs = ["test-unit", "test-integration"]     # waits for both
```

### Aspects (cross-cutting)

```toml
formula = "security-scan"
type = "aspect"

[[advice]]
target = "*.deploy"          # match every deploy step

[advice.before]
id = "security-scan-{step.id}"
title = "Security scan before {step.title}"
```

### Bond points (named attachment sites)

```toml
[[compose.bond_points]]
id = "entry"
description = "Attach setup work here"
before_step = "design"       # or after_step, plus optional parallel = true
```

> Step-completion hooks are **not** a formula feature. `on_complete.run` is not
> a valid field; if you see it in an old example, it never worked.

Unknown TOML keys are dropped **silently**. Always verify with
`bd formula show <name> --json` before pouring.

---

## 3. Cooking

```bash
bd cook release.formula.toml                  # compile mode: keep {{vars}}
bd cook release --var version=1.2.0           # runtime mode: substitute
bd cook release --mode=runtime --var version=1.2.0
bd cook release --dry-run                     # preview the steps
bd cook release.formula.toml --persist        # write the proto into the DB
bd cook release.formula.toml --persist --force
```

- **compile mode** (default) keeps placeholders — for modeling, estimation,
  and handoff.
- **runtime mode** (implied by any `--var`) fully resolves — for final
  validation before pouring.

By default cook prints the resolved formula to stdout; nothing is written to
the database. `--persist` creates a proto bead named after the formula with the
`template` label and a child per step. For most workflows you do not need
`--persist` at all: `bd mol pour` and `bd mol wisp` accept a formula name and
cook inline.

---

## 4. Molecules

### Creating

```bash
# From a formula (cooked inline)
bd mol pour release --var version=1.2.0
bd mol pour mol-feature --var name=auth --assignee agent-1
bd mol pour mol-feature --dry-run

# Without any formula — an epic plus edges IS a molecule
bd create "Feature X" -t epic
bd create "Design"    -t task --parent <epic>
bd create "Implement" -t task --parent <epic>
bd dep add <implement> <design>
```

If an ad-hoc epic turns out to be worth repeating, extract a formula from it:

```bash
bd mol distill <epic-id> my-workflow --var branch=feature-auth
```

### Executing

Children are **parallel by default**; only explicit dependencies sequence them.

```bash
bd ready --mol <mol-id>          # steps that can run right now
bd update <step-id> --claim
# ...do the work...
bd close <step-id> --reason "..."
bd close <step-id> --continue    # close and auto-advance to the next step
bd close <step-id> --continue --no-auto   # show the next step, don't claim it
```

### Inspecting

```bash
bd mol show <id>                 # structure and variables
bd mol show <id> --parallel      # which steps can run concurrently
bd mol current [<id>]            # [done]/[current]/[ready]/[blocked]/[pending]
bd mol current --for <agent>     # where that agent is
bd mol current <id> --range 100-150      # large molecules
bd mol progress <id>             # completed/total, rate, ETA (indexed, cheap)
bd mol last-activity <id>        # detect stuck molecules
bd mol stale [--blocking] [--unassigned]  # complete but still open
bd mol ready --gated             # molecules whose gate just closed
bd dep tree <id>                 # the full hierarchy
```

### Finishing

Closing the last child does **not** close the molecule root — epics stay open
as close-eligible work. Then:

```bash
bd mol squash <id> --summary "Agent-written summary of what happened"
bd mol squash <id> --keep-children
bd mol burn <id> --force          # delete outright, no digest (abandoned runs)
bd epic close-eligible            # sweep completed epics closed
```

`squash` collects the molecule's ephemeral children, writes a permanent digest
bead, and clears the ephemeral flag on the children (promoting them) or deletes
them. Supply `--summary` yourself — bd is a tool, not a summarizer; without it
you get a plain concatenation.

### Bonding — connecting two work graphs

```bash
bd mol bond A B                      # B depends on A (sequential, default)
bd mol bond A B --type parallel
bd mol bond A B --type conditional   # B runs only if A fails
bd mol bond A B --as "Compound title"
```

Polymorphic over operands:

| A + B | Result |
|---|---|
| proto + proto | compound proto (reusable) |
| proto + molecule | spawns the proto as new beads attached to the molecule |
| molecule + molecule | one compound molecule |
| formula + anything | the formula is cooked inline first |

Spawned beads follow the target's phase; override with `--pour` (force
persistent) or `--ephemeral` (force ephemeral). Classic use: a real bug found
during an ephemeral patrol —
`bd mol bond mol-critical-bug wisp-patrol --pour`.

**Dynamic bonding** — when the number of children is only known at runtime,
`--ref` gives readable child IDs instead of hashes:

```bash
bd mol bond mol-worker-arm bd-patrol --ref 'arm-{{name}}' --var name=ace
# creates bd-patrol.arm-ace (and children like bd-patrol.arm-ace.capture)
```

---

## 5. Gates

A gate is a bead that blocks its waiters until an external condition is met.
Agents never poll or spin: the blocked step simply leaves the ready frontier.

| Type | Waits for | Closed by |
|---|---|---|
| `human` | a person's decision | `bd gate resolve` only |
| `timer` | a duration after gate creation | `bd gate check` once elapsed |
| `gh:run` | a GitHub Actions run completing successfully | `bd gate check` (uses `gh run view`) |
| `gh:pr` | a PR merging | `bd gate check` (uses `gh pr view`) |
| `bead` | a bead in another rig closing | **cannot be auto-checked** — multi-rig routing was removed; resolve manually |

Resolution rules: `gh:run` resolves on `status=completed AND
conclusion=success` and **escalates** on failure/cancel; `gh:pr` resolves on
`state=MERGED` and escalates on `CLOSED`; `timer` resolves when
`now > created_at + timeout`.

Timeouts use Go duration syntax: `30m`, `1h`, `24h`. **There is no `d` unit** —
write `24h`, not `1d`.

### Ad-hoc gates

```bash
bd gate create --blocks bd-abc                              # human (default)
bd gate create --type=human --blocks bd-abc --reason "Design sign-off"
bd gate create --type=timer --blocks bd-abc --timeout=2h
bd gate create --type=gh:pr  --blocks bd-abc --await-id=42
bd gate create --type=gh:run --blocks bd-abc --await-id=<run-id>
bd gate add-waiter <gate-id> <issue-id>      # more beads wait on the same gate
```

A gate is a bead, so you can also wire it by hand:
`bd dep add <blocked-issue> <gate-id>`.

### Checking and resolving

```bash
bd gate list [--all]
bd gate show <gate-id>
bd gate check                    # evaluate all open gates, close satisfied ones
bd gate check --type=gh:pr --dry-run
bd gate check --escalate         # surface gates whose condition failed
bd gate resolve <gate-id> --reason "Approved by team lead"
bd gate discover                 # match gh:run gates to runs (SHA/branch/timing)
bd gate discover --dry-run --branch main --limit 10
```

Automate `bd gate check` from cron, a CI step, or a session-start hook —
`*/5 * * * * cd /path/to/repo && bd gate check`. Keep `human` gates manual.

### Gates in formulas

```toml
[[steps]]
id = "wait-for-ci"
title = "Wait for release workflow"

[steps.gate]
type = "gh:run"
id = "release.yml"        # which workflow to watch
timeout = "30m"           # escalate if it takes longer
```

Gate block schema: `type`, `id`, `await_id`, `timeout`, `repo`.

Cross-repository GitHub gates set `repo = "OWNER/REPO"` (or
`HOST/OWNER/REPO`); the value may be a `{{var}}` substituted at pour time.
Malformed values are rejected rather than silently falling back to the current
repo. An ad-hoc `gh:*` gate inherits a valid `metadata.repo` from the bead it
blocks; `human`/`timer`/`bead` gates do not. `bd gate discover` across repos
requires a workflow-name hint and ignores the local branch unless `--branch` is
passed explicitly.

Human sign-off and cooling-off timer:

```toml
[[steps]]
id = "approve-deploy"
title = "Human approves the deploy"
[steps.gate]
type = "human"

[[steps]]
id = "wait-24h"
title = "Let the release bake"
[steps.gate]
type = "timer"
timeout = "24h"
```

### Gates vs dependencies

Waiting on **other steps** is a dependency (`needs` / `waits_for`), not a gate.
Gates are for conditions **outside** the graph.

Gates also solve a real decoupling problem: with Dolt, closing a bead means
"work is done" but the code may still be on a branch awaiting review. The gate
makes the next bead wait on the *external* condition rather than on bead status.

Common recipe — PR merge handoff:

```bash
# Agent A
bd update issue-1 --claim
# ...write code, open PR #42...
GATE=$(bd gate create --type=gh:pr --blocks issue-2 --await-id=42)
bd close issue-1 --reason "Implemented; PR #42 open"

# Agent B
bd ready            # issue-2 absent (gate open)
bd gate check       # after the PR merges, the gate closes
bd ready            # issue-2 appears
```

---

## 6. Wisps — ephemeral molecules

Operational work whose beads are worthless the moment they close: release runs,
health patrols, diagnostics.

```bash
bd mol wisp <proto-id> [--var k=v]      # instantiate as vapor
bd mol wisp <proto-id> --root-only      # root only, no step children
bd create "One-off check" --ephemeral   # ad-hoc single wisp
bd mol wisp list [--all] [--type t]
bd mol wisp gc [--age 24h] [--closed] [--exclude-type agent,rig] --force
bd purge --force                        # delete ALL closed ephemeral beads
bd promote <wisp-id> --reason "Worth tracking long-term"
```

Properties: real beads in the main database with `Ephemeral=true`; worked with
normal `bd` commands; excluded from federation push by default
(`federation.exclude_types` defaults to `[wisp]`); deleted wholesale by
`bd purge` or `bd mol wisp gc`.

| | `bd mol pour` (molecule) | `bd mol wisp` (wisp) |
|---|---|---|
| Persistence | permanent, part of history | ephemeral, purged when done |
| Sync | synced like any bead | excluded from federation push |
| Use for | feature work, anything referenced later | release runs, operational loops, health checks |

A formula can declare `phase = "vapor"` to recommend wisp instantiation;
pouring a vapor-phase formula warns.

Practice: **squash before you delete.** If a wisp surfaced something durable,
`bd mol squash` promotes it; `bd mol burn` is irreversible. GC regularly.

---

## 7. Swarms — parallel execution of an epic

```bash
bd swarm validate <epic-id> [--verbose]     # run this FIRST
bd swarm create <epic-id> [--coordinator my-project/witness] [--force]
bd swarm list
bd swarm status <epic-or-swarm-id> [--json]
```

`bd swarm create` builds a swarm molecule (`mol_type=swarm`) linked to the epic
that any coordinator agent can pick up; a single non-epic bead is auto-wrapped
in an epic first. `bd swarm status` is **computed** from the beads — completed
/ active (with assignee) / ready / blocked — so it can never drift from
reality.

`bd swarm validate` is the pre-flight: dependency direction, orphaned roots,
missing edges, cycles, disconnected subgraphs, plus ready fronts, estimated
worker-sessions, and maximum parallelism.

---

## 8. `bd todo` — the lightweight surface

TODOs are not a separate system; they are task-type beads with shortcuts.

```bash
bd todo                      # list open task-type beads
bd todo list --all --json
bd todo add "Fix the login bug" -p 1 -d "Details"
bd todo done <id> [<id>...] [--reason "Fixed in PR #42"]
```

Promote a TODO the moment it turns out to be real work:
`bd update <id> --type bug --priority 0 --description "..."`.

Use `bd todo` for quick, informal capture and `bd create -t task` when the item
needs context, acceptance criteria, or dependencies.

---

## 9. Worked example: a release pipeline

```toml
# .beads/formulas/release.formula.toml
formula = "release"
description = "Standard release workflow"
version = 1
phase = "vapor"            # recommend wisp: a release run has no audit value

[vars.version]
required = true
pattern = "^\\d+\\.\\d+\\.\\d+$"

[[steps]]
id = "bump-version"
title = "Bump version to {{version}}"

[[steps]]
id = "changelog"
title = "Update CHANGELOG for {{version}}"
needs = ["bump-version"]

[[steps]]
id = "test"
title = "Run full test suite"
needs = ["changelog"]

[[steps]]
id = "tag"
title = "Create git tag v{{version}}"
needs = ["test"]

[[steps]]
id = "wait-for-ci"
title = "Wait for the release workflow"
needs = ["tag"]
[steps.gate]
type = "gh:run"
id = "release.yml"
timeout = "30m"

[[steps]]
id = "announce"
title = "Announce {{version}}"
needs = ["wait-for-ci"]
[steps.gate]
type = "human"
```

```bash
bd formula show release --json          # confirm the parser saw both gates
WISP=$(bd mol wisp release --var version=1.2.0)

bd ready --mol "$WISP"                  # only bump-version
bd update <step> --claim && bd close <step> --continue
# ...through tag...

bd gate check                           # closes wait-for-ci when CI goes green
bd ready --gated                        # this wisp is ready to resume
bd gate resolve <announce-gate> --reason "Comms approved"

bd mol squash "$WISP" --summary "Released 1.2.0; CI green; announced."
bd purge --force                        # reclaim the closed ephemeral beads
```
