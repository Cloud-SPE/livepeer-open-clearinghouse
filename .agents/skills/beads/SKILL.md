---
name: beads
description: >
  Use beads (the `bd` CLI) as the persistent, dependency-aware work tracker for
  this project — instead of TodoWrite, markdown checklists, or scratch plan
  files. Load this whenever work spans more than one step or session, has
  blockers or ordering, needs to survive context compaction or a crashed
  session, or is shared with other agents. Triggers: "track this", "create a
  task/issue/epic", "what's ready", "what's blocked", "resume where we left
  off", "plan this feature", "bd ", "beads", ".beads/", "bd ready", "bd prime".
  Also load before initializing beads in a new repo or debugging a beads,
  Dolt-sync, or `.beads/` problem. This skill is about USING beads to track
  work; it is not for authoring the beads project's own documentation (that is
  upstream's separate `beads-docs` skill, which only applies inside a checkout
  of the beads repository itself).
allowed-tools: Read, Bash(bd:*), Bash(git:*)
---

# Beads — persistent work graph for coding agents

Beads (`bd`) is a Dolt-backed issue tracker built for AI agents. Every unit of
work is a **bead** with an ID, a type, a priority, and typed edges to other
beads. `bd ready` computes the claimable frontier of that graph, so the tracker
— not your memory of the conversation — decides what is workable next. Work
survives session end, context compaction, and machine switches.

**Read this file, then open only the reference you need.** The `references/`
files are exhaustive; this page is the operating manual.

---

## The rules that matter most

These are non-negotiable in a beads project. Violating them silently loses work.

1. **Beads is the only task tracker.** Do not use TodoWrite, TaskCreate,
   `TODO.md`, `PLAN.md`, or checklists in chat for tracked work. One system,
   one source of truth.
2. **Create the bead before writing the code.** A bead created after the fact
   is a changelog entry, not a plan, and it cannot block or unblock anything.
3. **Run `bd prime` at session start and after every compaction/clear.** It
   prints the current workflow contract plus your stored memories. Hooks
   normally do this for you; if you are unsure whether it ran, run it.
4. **Never run `bd edit`.** It opens `$EDITOR` and hangs a non-interactive
   agent forever. Same for `bd create-form`. Use `bd update --description=…`,
   `--notes=…`, `bd note`, or `--body-file`.
5. **Use `--json` for anything you parse.** Human output is a rendering, not an
   API. `bd list --json`, `bd show <id> --json`, `bd ready --json`.
6. **Every bead gets a real description.** "Fix auth bug" tells a future agent
   nothing. Say what is broken, where, and what done looks like.
7. **Dependency direction is *requirement*, never *time*.**
   `bd dep add <dependent> <blocker>` reads "**A needs B**". "Phase 1 before
   Phase 2" is `bd dep add phase2 phase1`. Verify with `bd blocked`.
8. **File discovered work as you find it**, linked to what you were doing:
   `bd create "…" --deps discovered-from:<current-id>`. Do not silently fix
   unrelated things, and do not drop them on the floor.
9. **Close what you finish, with a reason**, and close in one call:
   `bd close bd-a1 bd-b2 --reason "…"`. Unclosed blockers block everything
   behind them forever.
10. **Never treat `.beads/issues.jsonl` as the database or as sync.** It is an
    optional export. Sync is `bd dolt push` / `bd dolt pull`.
11. **Never run raw `dolt` commands against a live workspace**, and never
    delete anything inside a `.dolt/` directory. Both corrupt the store. Use
    `bd dolt …`.
12. **Respect git authority.** `bd prime` states whether you may commit, push,
    or run `bd dolt push` in this project. Default is conservative: do the
    beads work, then report what you would run.

---

## The session loop

This is the whole workflow. Everything else is detail.

```bash
# 1. ORIENT — always, at session start and after compaction
bd prime                       # workflow contract + your persistent memories
bd ready                       # what is claimable right now
bd list --status in_progress   # what was already claimed (possibly by you)

# 2. PICK — claim atomically so two agents can't take the same bead
bd show bd-a1b2 --json         # read the whole bead before starting
bd update bd-a1b2 --claim      # sets assignee=you AND status=in_progress
#   or, self-service in one shot:
bd ready --claim --json

# 3. WORK — record what you learn, as you learn it
bd note bd-a1b2 "Root cause: token refresh races the retry loop"
bd create "Extract validateToken helper" -t chore -p 3 \
  --description="Found while fixing bd-a1b2; duplicated in 3 call sites." \
  --deps discovered-from:bd-a1b2

# 4. FINISH — close, then look at what that unblocked
bd close bd-a1b2 --reason "Fixed in <commit/PR>; added regression test"
bd ready                       # newly unblocked work appears here

# 5. HAND OFF — persist across machines/agents (if git authority allows)
bd dolt push
```

**Starting a new piece of work instead of picking one up:**

```bash
bd create "Add OAuth login" -t feature -p 1 \
  --description="Users can sign in with Google. Replaces the password form." \
  --acceptance="Login works for new + existing users; refresh token rotates"
```

**Planning something big** — one epic, children, then real edges:

```bash
EPIC=$(bd q "Auth system rewrite" -t epic -p 1)     # bd q prints only the ID
bd create "Design token schema"  -p 1 --parent "$EPIC"   # → $EPIC.1
bd create "Implement endpoints"  -p 1 --parent "$EPIC"   # → $EPIC.2
bd create "Migrate existing users" -p 1 --parent "$EPIC" # → $EPIC.3

bd dep add "$EPIC.2" "$EPIC.1"     # endpoints NEED the schema
bd dep add "$EPIC.3" "$EPIC.2"     # migration NEEDS the endpoints

bd dep tree "$EPIC"                # verify the shape
bd ready --parent "$EPIC"          # only .1 should be ready
```

Children of an epic are **parallel by default**. Numbering them "Step 1/2/3"
creates no ordering — only `bd dep add` does.

---

## Command surface you actually need

`bd --help` lists 109 top-level commands on 1.2.2. These twenty cover ~95% of
agent work.

| Command | Use it for |
|---|---|
| `bd prime` | Session context + memories. Run first, and after compaction. |
| `bd ready [--json] [--claim]` | The claimable frontier. Add `--explain` to see why. |
| `bd blocked` | What is stuck, and on what. |
| `bd create "<title>" -t <type> -p <n> -d "<desc>"` | New bead. |
| `bd q "<title>"` | Quick capture; prints only the ID (script-friendly). |
| `bd show <id> [--json] [--long]` | Full bead: fields, deps, counts. |
| `bd list --status open --json` | Filtered listing (50 rows by default). |
| `bd search "<text>"` | Find by title/ID before creating a duplicate. |
| `bd update <id> --claim` | Atomic claim (assignee + in_progress). |
| `bd update <id> --priority 0 --add-label urgent` | Field edits, no editor. |
| `bd close <id...> --reason "…"` | Finish work. Accepts several IDs. |
| `bd note <id> "<text>"` | Append to notes (progress, findings). |
| `bd comment <id> "<text>"` | Discussion/handoff message on a bead. |
| `bd dep add <dependent> <blocker>` | "dependent NEEDS blocker". |
| `bd dep tree <id>` | Visualize what blocks what. |
| `bd label add <id> <label>` | Cross-cutting tagging. |
| `bd remember "<insight>"` | Project memory that `bd prime` re-injects. |
| `bd status` (alias `bd stats`) | Counts, ready work, recent activity. |
| `bd doctor` | Health check. `--fix` only after backing up. |
| `bd dolt push` / `bd dolt pull` | Cross-machine sync. |

Priorities are **0–4, numeric**: 0 critical, 1 high, 2 medium (default), 3 low,
4 backlog. Never pass "high"/"medium"/"low".

Built-in types on 1.2.2: `task` (default), `bug`, `feature`, `chore`, `epic`,
`decision`, `spike`, `story`, `milestone`. Extra types need
`bd config set types.custom "…"`.

Built-in statuses: `open` (active), `in_progress` (wip), `blocked` (wip),
`deferred` (frozen), `closed` (done), `pinned` (frozen), `hooked` (wip).
Only `active` statuses appear in `bd ready`.

---

## Dependencies in one table

| Type | Blocks `bd ready`? | Meaning |
|---|---|---|
| `blocks` *(default)* | **yes** | Hard ordering: blocker must close first. |
| `parent-child` | indirectly | Epic/subtask; a blocked parent blocks children. |
| `conditional-blocks` | yes | Runs only if the other side failed. |
| `waits-for` | yes | Waits for all/any of another bead's children. |
| `discovered-from` | no | Provenance: found while doing that. |
| `related`, `tracks`, `caused-by`, `validates`, `supersedes` | no | Annotations. |

A bead is ready when it is open, unassigned to a wip/frozen status, not
deferred, not gated, and **every** blocking dependency is closed.

---

## Recovering after compaction or a crash

You lost the conversation, not the work. In order:

```bash
bd prime                            # re-read the contract + memories
bd list --status in_progress --json # what you had claimed
bd show <id> --json                 # description, design, notes, acceptance
bd comments <id>                    # handoff notes from other agents
bd ready                            # if nothing was claimed, start fresh here
```

If a bead was claimed but the work never happened, either continue it or
release it: `bd update <id> --status open` and clear the assignee with
`bd assign <id> ""`.

This is *why* every bead needs a description and running notes: the bead is
the memory. Add `bd remember "…"` for facts that outlive a single bead
("this repo's integration tests need Docker running").

---

## Anti-patterns that break agents

| Don't | Do instead |
|---|---|
| `bd edit <id>` (opens vim, hangs) | `bd update <id> --description=…` / `bd note` |
| `bd create-form` | `bd create` with flags |
| Parsing human output with grep | `--json` + `jq` |
| `-s open -s closed` (repeats overwrite) | `--status open,closed` |
| `bd import`/`bd export` as sync | `bd dolt push` / `bd dolt pull` |
| Committing `.beads/embeddeddolt/` | It is gitignored by `bd init`; keep it so |
| `dolt sql-server` / `dolt remote add` by hand | `bd dolt start` / `bd dolt remote add` |
| Deleting `.dolt/noms/LOCK` to fix a lock | `bd dolt stop`, then `bd doctor` |
| Creating a near-duplicate bead | `bd search "<title>"` first |
| Closing an epic's last child and walking away | epics stay open: `bd epic close-eligible` |
| `bd doctor --fix` on a hunch | back up `.beads/`, `bd doctor --dry-run`, then fix |
| Marking work done with unclosed beads | run the close protocol from `bd prime` |

---

## Reference index

Open these on demand — they are complete, not summaries.

| File | Covers |
|---|---|
| [`references/01-installation-and-init.md`](references/01-installation-and-init.md) | Installing `bd`, every `bd init` flag, embedded vs server mode, first-run verification, upgrading, uninstalling |
| [`references/02-core-concepts.md`](references/02-core-concepts.md) | Bead anatomy, every field, types, priorities, statuses, hash + hierarchical IDs, labels, metadata, graph links |
| [`references/03-dependencies-and-ready.md`](references/03-dependencies-and-ready.md) | All dependency types, ready-work semantics, `bd blocked`, cycles, graph visualization, cross-repo edges |
| [`references/04-cli-reference.md`](references/04-cli-reference.md) | Every command grouped by task, with the flags that matter |
| [`references/05-workflows.md`](references/05-workflows.md) | Formulas → protos → molecules/wisps, gates, bonding, swarms, epics, `bd todo` |
| [`references/06-agent-playbook.md`](references/06-agent-playbook.md) | Session protocols, issue-writing standards, discovered work, memories, context recovery, worked end-to-end examples |
| [`references/07-multi-agent.md`](references/07-multi-agent.md) | Claiming, leases, merge slots, handoff, routing, hydration, federation, bucket federation |
| [`references/08-sync-and-storage.md`](references/08-sync-and-storage.md) | Dolt model, push/pull, bootstrap, remotes, backup/restore, JSONL, git hooks, worktrees, pruning |
| [`references/09-configuration.md`](references/09-configuration.md) | Config precedence, every key worth setting, env vars, secrets, agent policy profiles |
| [`references/10-harness-integration.md`](references/10-harness-integration.md) | Claude Code, Codex, MCP server, `bd setup` recipes, hook wiring |
| [`references/11-troubleshooting.md`](references/11-troubleshooting.md) | `bd doctor`, init-safety refusals, corruption, sync failures, sandboxes, every recovery runbook |
| [`references/12-json-and-scripting.md`](references/12-json-and-scripting.md) | JSON contract, query language, `bd sql`, `bd batch`, events journal, observability |

**Where to start for a common ask:**

- "Set beads up in this repo" → 01, then 10.
- "Plan this feature" → 06 (issue standards) + 03 (edges).
- "What should I work on?" → this page's session loop.
- "Something is broken / `bd` refuses" → 11.
- "Two agents / two machines" → 07 and 08.
- "Build a repeatable pipeline" → 05.

---

## Attribution

Beads is MIT-licensed (© 2025 Beads Contributors), created by Steve Yegge —
<https://github.com/gastownhall/beads>.
This skill is an independent write-up derived from that project's public
documentation; see `NOTICE.md` in this repository. Command behaviour here was
verified against the versions recorded in `BEADS_VERSION.md`. If `bd` disagrees
with this skill, `bd help <command>` wins — and the skill needs updating.
