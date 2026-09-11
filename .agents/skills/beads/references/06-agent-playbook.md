# 06 — Agent playbook

How an agent should actually behave in a beads project. This is the file to
read when the question is "what do I do", not "what does the flag do".

---

## 1. Session protocol

### Start of session

```bash
bd prime            # ALWAYS. The workflow contract + your persistent memories.
```

`bd prime` prints ~1–2k tokens: the session close protocol, the core rules, the
essential command set, the **git authority** for this project, and every
memory stored with `bd remember`. In Claude Code and Codex a SessionStart hook
runs it automatically; SessionStart also fires after compaction. If you cannot
tell whether it ran, run it — it is cheap and idempotent.

If your host truncates hook output into a preview file, **read the full file**
before continuing. `bd prime` says so in its first line for exactly this reason.

Then orient:

```bash
bd ready                        # the claimable frontier
bd list --status in_progress    # what is already claimed, and by whom
bd blocked                      # what is stuck (often reveals a stale claim)
bd status                       # counts + recent activity, for a quick read
```

### During the session

- One claimed bead at a time, unless the work genuinely parallelizes.
- Record findings on the bead as you go — `bd note`, not chat scrollback.
- File discovered work immediately with `discovered-from`.
- Re-run `bd ready` after every close.

### End of session — the close protocol

`bd prime` prints the authoritative checklist for the project. The general
shape:

```
[ ] bd close <id...>          — close everything actually finished
[ ] bd ready                  — confirm nothing you finished is still open
[ ] bd dolt push              — if (and only if) git authority allows
```

Never say "done" with beads left `in_progress` that you actually completed, and
never close beads you did not finish — reopen or note the state instead.

### Git authority

`bd prime` states one of these, and it overrides your instincts:

| Profile | Meaning |
|---|---|
| `conservative` (default) | Track work in beads, then **report** the files changed, validation run, and the commands you propose. Do not commit, push, or `bd dolt push` without explicit approval. |
| `minimal` | Same authority; the installed instructions are just shorter. |
| `team-maintainer` | You may close beads, run quality gates, commit, `bd dolt push`, and `git push` as routine work. |

Hard constraints always win: stealth mode, no git remote, an ephemeral branch,
or `no-push` config each remove authority regardless of profile. An explicit
"do not commit" from the user outranks everything. Beads never infers
team-maintainer authority just because a remote exists — someone set
`agent.profile` or `BD_AGENT_PROFILE` deliberately.

---

## 2. Writing a bead worth reading later

The bead **is** the memory. Write for an agent who has none of your context.

### Bad

```bash
bd create "Fix auth bug" -t bug -p 1
```

### Good

```bash
bd create "Login fails when the password contains a double quote" \
  -t bug -p 1 -l auth,backend \
  --description="POST /login 500s when the password contains \". The
query builder in internal/auth/query.go interpolates rather than binds.
Repro: curl -d 'password=a\"b' localhost:8080/login" \
  --acceptance="Passwords with quotes authenticate; regression test added
in auth_test.go; no other interpolated queries remain in that file." \
  --design="Bind parameters instead of interpolating; audit the file for
sibling cases."
```

Checklist for every bead:

- **Title** — specific enough to be recognized in a list of 50. Not "fix bug",
  not "update code".
- **Description** — what is wrong or wanted, where in the codebase, and how to
  reproduce or verify. Include file paths.
- **Acceptance criteria** — what "done" means. `bd lint` checks these by type:
  `bug` wants Steps to Reproduce + Acceptance Criteria; `task`/`feature` want
  Acceptance Criteria; `epic` wants Success Criteria; `chore` needs neither.
- **Type and priority** — numeric priority, real type.
- **Labels** — component and domain, so it can be found by filter.
- **Dependencies** — if it cannot start yet, say what it needs.

Enforce it project-wide:

```bash
bd config set validation.on-create warn     # or `error`
bd config set create.require-description true
bd create "..." --validate                  # one-off check
bd lint                                     # audit existing open beads
```

### Search before you create

Duplicate beads are the standard agent failure mode.

```bash
bd search "authentication"
bd list --json | jq -r '.[].title'
bd find-duplicates --threshold 0.4
```

If you created one anyway: `bd duplicate <dup> --of <canonical>`.

---

## 3. Discovered work

When you find something while doing something else, the correct move is
**never** "fix it silently" and **never** "mention it in chat".

```bash
bd create "Extract validateToken helper" -t chore -p 3 \
  --description="Duplicated in auth/login.go, auth/refresh.go, and
middleware/session.go. Found while fixing bd-a1b2." \
  --deps discovered-from:bd-a1b2
```

`discovered-from` does not block anything — it records provenance so a human
can see why the bead exists. In multi-repo setups, a bead created with
`discovered-from` inherits its parent's `source_repo`, so discovered work stays
attributed to the project that produced it (override with `--repo`).

Escalation rule of thumb:

| Finding | Action |
|---|---|
| Trivial and in-scope (typo in the line you're editing) | Just fix it |
| Related but out of scope | `bd create … --deps discovered-from:<current>` |
| Blocks your current bead | Create it, then `bd dep add <current> <new>` and re-check `bd ready` |
| Needs a human decision | `bd create … -l human`, then `bd human list` surfaces it |

---

## 4. Persistent memory

Beads carry work. `bd remember` carries facts that outlive any one bead.

```bash
bd remember "Integration tests need Docker running; make test-int starts it"
bd remember "Dolt phantom DBs hide in three places" --key dolt-phantoms
bd memories                 # list all
bd memories dolt            # search
bd recall dolt-phantoms     # read one
bd forget dolt-phantoms     # delete
```

Memories are injected by `bd prime` at the top of every session. Use them for
build/test invocations, environment quirks, architectural decisions, and
conventions. **Do not create `MEMORY.md` / `NOTES.md` files** — they fragment
across sessions and accounts, and nothing re-injects them.

Keep them short and high-value: `prime.max-memories` and
`prime.max-memory-chars` exist because an unbounded memory list eats the
context window it was meant to save.

Memories are excluded from `bd export` unless `--include-memories` or `--all`
(they may contain agent context you would not want in an interchange file).

---

## 5. Recovering from compaction, a crash, or a cold start

You lost the conversation. The work is intact.

```bash
bd prime                                  # 1. re-read the contract + memories
bd list --status in_progress --json       # 2. what you had claimed
bd show <id> --json                       # 3. description, design, notes, acceptance
bd comments <id>                          # 4. handoff notes from other agents
bd dep tree <id>                          # 5. how it fits the plan
bd ready                                  # 6. if nothing was claimed, start here
```

If a claimed bead was never worked, release it so someone else can take it:

```bash
bd update <id> --status open
bd assign <id> ""
```

If it was partly worked, say so on the bead before stopping:

```bash
bd note <id> "Refactor half-done: query.go converted to bound params,
refresh.go not yet. Tests currently failing in auth_test.go:112."
```

That note is the difference between a resumable bead and a landmine.

---

## 6. Multi-step feature, end to end

```bash
# --- PLAN -------------------------------------------------------------
bd search "dark mode"                      # not already tracked?

EPIC=$(bd q "Dark mode" -t epic -p 2)
bd update "$EPIC" --description="User-selectable dark theme with system
preference detection and persistence." \
  --acceptance="Toggle in settings; respects prefers-color-scheme on first
load; choice persists across reloads; no contrast regressions."

bd create "Define dark palette tokens" -p 2 --parent "$EPIC" \
  --description="Add semantic color tokens to styles/tokens.css." \
  --acceptance="Every hard-coded hex in components/ maps to a token."

bd create "Wire theme switching" -p 2 --parent "$EPIC" \
  --description="Toggle in Settings; persist to localStorage; honor
prefers-color-scheme when nothing is stored."

bd create "Audit contrast" -p 3 --parent "$EPIC" \
  --description="Check every surface against WCAG AA in both themes."

bd dep add "$EPIC.2" "$EPIC.1"     # wiring NEEDS tokens
bd dep add "$EPIC.3" "$EPIC.2"     # audit NEEDS wiring

bd dep tree "$EPIC"                # verify the shape
bd ready --parent "$EPIC"          # only .1 should be ready

# --- EXECUTE ----------------------------------------------------------
bd update "$EPIC.1" --claim
# ...work...
bd note "$EPIC.1" "Tokens land in styles/tokens.css; 3 components still
hard-code #fff — filed separately."
bd create "Replace hard-coded #fff in legacy components" -t chore -p 3 \
  --deps discovered-from:"$EPIC.1" \
  --description="Button, Card, and Modal bypass the token layer."
bd close "$EPIC.1" --reason "Palette tokens defined and documented" --suggest-next

bd ready --parent "$EPIC"          # .2 is now ready

# --- FINISH -----------------------------------------------------------
bd close "$EPIC.2" "$EPIC.3" --reason "Dark mode shipped"
bd epic status --eligible-only
bd epic close-eligible
bd dolt push                       # if git authority allows
```

---

## 7. Parallel agents on one epic

```bash
bd swarm validate "$EPIC"                 # check the DAG before fanning out
bd swarm create "$EPIC"
bd swarm status "$EPIC"

# Each worker, independently:
bd ready --parent "$EPIC" --claim --json  # atomic; first claim wins
```

Prefer `--claim` over `bd assign` when agents self-select work: claiming is
atomic and idempotent, assignment is a race. Details, leases, and merge slots
are in `07-multi-agent.md`.

---

## 8. Working with epics and long-running plans

- Keep epics shallow: epic → task → subtask (3 levels is the configured max).
- Give the epic acceptance criteria, not just a title.
- Children are parallel unless you add edges — add them deliberately.
- Watch for the completed-but-open epic: `bd epic status --eligible-only`,
  `bd mol stale`.
- `bd stale --days 14` finds beads nobody has touched; triage or close them.
- `bd orphans` finds beads referenced in commit messages but still open —
  work that shipped without being closed. `bd orphans --fix` closes them after
  confirmation.
- `bd preflight --check` before opening a PR: lint, stale, orphans, and
  common repo hygiene.

---

## 9. Quality gates via labels

A lightweight review workflow that needs no extra tooling:

```bash
bd update <id> --claim
bd label add <id> needs-tests
bd label add <id> needs-docs
# ...work...
bd label remove <id> needs-tests
bd label remove <id> needs-docs
bd label add <id> needs-review
bd label add <id> ai-generated          # honesty about provenance
bd close <id> --reason "..."            # only once the gates are clear
```

Reviewers find work with `bd list --label needs-review --status in_progress`.
For a hard block (the bead genuinely must not proceed), use a `human` gate
instead of a label — see `05-workflows.md`.

---

## 10. Agent anti-patterns, ranked by damage

1. **Tracking work outside beads.** TodoWrite, a markdown plan, or a chat list
   evaporates at compaction. Everything tracked goes in beads.
2. **Running `bd edit`.** Hangs the session on `$EDITOR`.
3. **Backwards dependencies.** "before/after" instead of "needs". Always verify
   with `bd blocked`.
4. **Beads with no description.** Unresumable; the next agent re-derives
   everything or does the wrong thing.
5. **Not closing finished work.** Every downstream bead stays blocked forever.
6. **Duplicate beads.** Search first.
7. **Parsing human output.** Use `--json`.
8. **Silently doing out-of-scope work.** File it as `discovered-from` instead.
9. **Using JSONL export/import as sync.** It is upsert-only and cannot
   represent deletes. Use `bd dolt push` / `bd dolt pull`.
10. **`bd doctor --fix` reflexively.** It can remove dependencies it believes
    are circular, including valid parent-child edges. Back up `.beads/`, run
    `--dry-run`, read the output, then fix.
11. **Committing or pushing without authority.** Check what `bd prime` said.
12. **Leaving a claimed bead claimed** when you stop. Release it or note the
    state.

---

## 11. A ready-made project instruction snippet

`bd onboard` prints the canonical minimal snippet. If you are writing an
`AGENTS.md` / `CLAUDE.md` section by hand, this is the shape:

```markdown
## Issue tracking (beads)

This project uses `bd` (beads) for all task tracking.

- Run `bd prime` at session start and after any context compaction.
- Find work with `bd ready`; claim it with `bd update <id> --claim`.
- Create a bead **before** writing code; always include `--description`.
- File work discovered mid-task: `bd create "..." --deps discovered-from:<id>`.
- Close with a reason: `bd close <id> --reason "..."`.
- Use `bd remember "insight"` for durable project knowledge — never MEMORY.md.
- Do NOT use TodoWrite or markdown checklists for tracked work.
- Do NOT run `bd edit` (it opens $EDITOR and will hang).
- Sync at session end with `bd dolt push` if git authority allows.
```
