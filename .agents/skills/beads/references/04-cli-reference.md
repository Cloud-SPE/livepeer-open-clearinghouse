# 04 — CLI reference

Every top-level `bd` command, grouped by what you are trying to do, with the
flags that matter in practice. `bd help <command>` is always authoritative;
`bd help --all` prints the whole thing.

`bd --help` lists 109 commands on 1.2.2, and that is the complete set.

> **Documented upstream but NOT in the 1.2.2 binary.** Upstream's prose docs
> live on `main`, which is ahead of the release, and describe several commands
> that 1.2.2 rejects with `Error: unknown command`: **`bd sync`**,
> **`bd events`**, **`bd serve`**, **`bd reclaim`**, **`bd unclaim`**,
> **`bd heartbeat`**. They are registered in `cmd/bd/*.go` on main, so they
> should ship in a later release. Until then, do not use them — and note that
> `bd help <unknown>` exits **0** while printing "Unknown help topic", so
> `bd help foo && echo ok` is not a valid existence test. Use
> `bd foo --help 2>&1 | head -1` and look for `Error: unknown command`.
> Sections that rely on these commands carry the same warning.

---

## Global flags (apply to every command)

| Flag | Effect |
|---|---|
| `--json` | JSON output. Use for anything you parse. |
| `-q, --quiet` | Errors only |
| `-v, --verbose` | Debug output |
| `--actor <name>` | Actor for the audit trail (default `$BEADS_ACTOR`, git `user.name`, `$USER`) |
| `--db <path>` | Override database path |
| `-C, --directory <dir>` | Run as if from this directory (like `git -C`) |
| `--dolt-auto-commit off\|on\|batch` | Dolt commit policy for this invocation. `batch` defers to `bd dolt commit` |
| `--global` | Use the shared-server global database (`beads_global`) |
| `--readonly` | Block writes — good for worker sandboxes |
| `--sandbox` | Sandbox mode: disables Dolt auto-push |
| `--ignore-schema-skew` | Proceed despite forward schema drift (some queries may fail) |
| `--profile` | Emit a CPU profile |

---

## Creating and editing beads

### `bd create`

```bash
bd create "<title>" -t <type> -p <n> -d "<description>"
```

| Flag | Purpose |
|---|---|
| `--title <s>` | Title as a flag instead of positional |
| `-d, --description <s>` | Description |
| `--body-file <f>` / `--stdin` | Read the description from a file or stdin (`-` = stdin) |
| `--design <s>` / `--design-file <f>` | Design notes |
| `--acceptance <s>` | Acceptance criteria |
| `--notes <s>` / `--append-notes <s>` | Notes |
| `--context <s>` | Extra context |
| `-t, --type <s>` | `task` (default), `bug`, `feature`, `epic`, `chore`, `decision`, … |
| `-p, --priority <n>` | 0–4 or P0–P4 (default 2) |
| `-l, --labels a,b` | Labels |
| `-a, --assignee <s>` | Assignee |
| `-e, --estimate <min>` | Estimate in minutes |
| `--parent <id>` | Create as a hierarchical child |
| `--no-inherit-labels` | Do not copy the parent's labels |
| `--deps type:id,…` | Dependencies at creation (`discovered-from:bd-20,blocks:bd-15`) |
| `--due <s>` / `--defer <s>` | `+6h`, `+1d`, `tomorrow`, `next monday`, `2026-01-15` |
| `--external-ref <s>` | `gh-9`, `jira-ABC`, Linear URL |
| `--metadata <json\|@file>` | Arbitrary metadata |
| `--id <s>` | Explicit ID (bypasses generation) |
| `--silent` | Print only the ID |
| `--json` | Structured result |
| `--dry-run` | Preview without creating |
| `--validate` | Check the description has the sections required for the type |
| `-f, --file <md>` | Batch-create from a markdown file |
| `--graph <json>` | Create a whole plan (beads + dependencies) from JSON |
| `--ephemeral` | Create as a wisp |
| `--no-history` | Skip Dolt commit history without becoming GC-eligible |
| `--waits-for <id>` / `--waits-for-gate all-children\|any-children` | Fan-in gate |
| `--repo <path>` | Override multi-repo routing |
| `--force` | Allow a prefix that does not match the database |
| `--spec-id`, `--skills`, `--mol-type`, `--wisp-type`, `--event-*` | Specialized fields |

### `bd q` — quick capture

```bash
ID=$(bd q "Fix login bug")            # prints ONLY the id
bd q "Task" -t bug -p 1 -l urgent
```

### `bd update`

Accepts several IDs. With no ID, updates the **last touched** bead.

| Flag | Purpose |
|---|---|
| `--claim` | Atomic: assignee = you, status = in_progress (idempotent) |
| `-s, --status <s>` | New status |
| `-p, --priority <n>` | New priority |
| `-t, --type <s>` | New type |
| `--title <s>` | New title |
| `-d, --description <s>` / `--body-file` / `--stdin` | Description |
| `--design <s>` / `--design-file <f>` / `--acceptance <s>` | Content fields |
| `--notes <s>` / `--append-notes <s>` | Notes (append preserves history) |
| `-a, --assignee <s>` | Assign (`""` clears) |
| `--add-label` / `--remove-label` / `--set-labels` | Label edits |
| `--set-metadata k=v` / `--unset-metadata k` / `--metadata <json>` | Metadata |
| `--parent <id>` | Reparent (`""` detaches) |
| `--due <s>` / `--defer <s>` | Scheduling (`""` clears) |
| `--ephemeral` / `--persistent` | Wisp ↔ persistent |
| `--no-history` / `--history` | Dolt history flag |
| `--await-id <s>` | Set a gate's await id |
| `-e, --estimate <min>`, `--external-ref`, `--spec-id` | Misc |
| `--allow-empty-description` | Permit clearing the description from stdin/file |

### Shorthands

```bash
bd assign <id> <name>        # = bd update <id> --assignee <name>;  "" unassigns
bd priority <id> <n>         # = bd update <id> --priority <n>
bd tag <id> <label>          # = bd update <id> --add-label <label>
bd note <id> "text"          # = bd update <id> --append-notes "text"  (--file/--stdin)
bd comment <id> "text"       # = bd comments add                       (--file/--stdin)
bd link <a> <b> [-t type]    # = bd dep add <a> <b>
```

### Closing / reopening / deleting

```bash
bd close <id...> --reason "..."      # alias: bd done
bd close <id> --suggest-next         # print what this unblocked
bd close <id> --claim-next           # close and claim the next highest-priority ready bead
bd close <id> --continue             # auto-advance to the next molecule step
bd close <id> --force                # close pinned beads / unsatisfied gates
bd close <id> --reason-file f        # reason from a file ("-" = stdin)
bd reopen <id...> --reason "..."
bd delete <id...> --force            # destructive; without --force it previews
bd delete <id> --cascade --force     # also delete dependents
bd delete --from-file ids.txt --dry-run
```

Closing multiple beads: one `--reason` applies to all, or repeat `--reason` once
per ID and they map positionally.

`bd delete` removes dependency links in both directions, rewrites text
references to `[deleted:ID]` in directly connected beads, and permanently
deletes the beads. It fails if a bead has dependents outside the deletion set
unless you pass `--cascade` (delete them too) or `--force` (orphan them).

### Never use interactively-blocking commands

`bd edit <id>` opens `$EDITOR`; `bd create-form` opens a TUI form. Both hang a
non-interactive agent. Use `bd update`, `bd note`, or `--body-file`.

---

## Finding work

```bash
bd ready [--json] [--claim] [--explain] [--priority n] [--type t]
         [--label a,b] [--label-any a,b] [--exclude-label x] [--exclude-type y]
         [--assignee a] [--unassigned] [--parent id] [--mol id] [--gated]
         [--sort priority|hybrid|oldest] [--limit n] [--plain|--pretty]
         [--include-deferred] [--include-ephemeral]

bd blocked [--parent id] [--json]
bd list [flags]
bd search "<text>" [flags]
bd query "<expression>" [flags]
bd count [--by-status|--by-priority|--by-type|--by-assignee|--by-label]
bd show <id...> [--json] [--long] [--short] [--children] [--refs] [--thread]
                [--include-comments] [--include-dependents] [--as-of <ref>]
                [--current] [-w/--watch] [--local-time]
bd children <parent-id> [--pretty]
bd stale [--days 30] [--status s] [--limit n]
bd status            # alias: bd stats
```

### `bd list` filters worth remembering

Status/type/priority: `-s/--status open,in_progress` (**comma-separated — a
repeated `-s` silently overwrites**), `-t/--type`, `-p/--priority`,
`--priority-min`, `--priority-max`, `--all` (include closed).

Labels: `-l/--label` (AND), `--label-any` (OR), `--exclude-label`,
`--label-pattern`, `--label-regex`, `--no-labels`.

Text: `--title`, `--title-contains`, `--desc-contains`, `--notes-contains`,
`--empty-description`.

Structure: `--parent <id>`, `--no-parent`, `--id a,b,c`, `--ready`,
`--include-gates`, `--include-infra`, `--include-templates`, `--mol-type`,
`--wisp-type`, `--spec`.

Assignment: `-a/--assignee`, `--no-assignee`.

Dates: `--created-after/-before`, `--updated-after/-before`,
`--closed-after/-before`, `--due-after/-before`, `--defer-after/-before`,
`--overdue`, `--deferred`.

Metadata: `--metadata-field k=v` (repeatable), `--has-metadata-key k`.

Output: `-n/--limit` (default 50, `0` = unlimited), `--sort <field>`,
`-r/--reverse`, `--long`, `--pretty`/`--tree` (default), `--flat`,
`--no-pager`, `--format digraph|dot|<go-template>`, `-w/--watch`,
`--skip-labels` (faster when you do not need labels), `--offset`
(proxied-server only).

### `bd query` — the compound query language

```bash
bd query "status=open AND priority<=2 AND updated>7d"
bd query "(status=open OR status=blocked) AND priority<2"
bd query "type=bug AND label=urgent"
bd query "assignee=none AND type=task"
bd query "NOT status=closed"
bd query "id=bd-*"
```

Operators `= != > >= < <=`, boolean `AND OR NOT` with parentheses.
Fields: `status`, `priority`, `type`, `assignee`, `owner`, `label`, `title`,
`description`, `notes`, `created`, `updated`, `started`, `closed`, `id`,
`spec`, `pinned`, `ephemeral`, `template`, `parent`, `mol_type`. Use `none` for
empty assignee/label/description. Dates accept `7d`, `24h`, `2w`,
`2026-01-15`, `tomorrow`, `"next monday"`. Flags: `-a/--all` (include closed),
`-n/--limit`, `--sort`, `-r/--reverse`, `--long`, `--parse-only` (show the AST).

### `bd search`

Searches title and ID, excluding closed by default. ID-like queries use fast
prefix matching. Supports the same label/date/priority/metadata filters as
`bd list`, plus `--desc-contains`, `--external-contains`, `--status all`.

---

## Dependencies and structure

```bash
bd dep add <dependent> <blocker> [--type <t>] [--no-cycle-check]
bd dep add --file deps.jsonl        # bulk; '-' for stdin
bd dep remove <dependent> <blocker> # alias: bd dep rm
bd dep list <id...> [--direction down|up] [--type t]
bd dep tree <id> [--direction down|up|both] [--status s] [--max-depth n]
                 [--format mermaid] [--show-all-paths]
bd dep cycles
bd dep relate <a> <b>               # bidirectional relates-to
bd dep unrelate <a> <b>
bd dep <blocker> --blocks <dependent>

bd graph [<id>] [--all] [--compact|--box|--dot|--html]
bd graph check                      # exit 0 clean, 1 problems

bd epic status [--eligible-only]
bd epic close-eligible [--dry-run]

bd duplicate <id> --of <canonical>
bd duplicates [--dry-run] [--auto-merge]
bd find-duplicates [--threshold 0.5] [--method mechanical|ai] [--limit n]
bd supersede <old> --with <new>

bd swarm create <epic-id> [--coordinator addr] [--force]
bd swarm list
bd swarm status <epic-or-swarm-id>
bd swarm validate <epic-id> [--verbose]
```

`bd swarm validate` checks dependency direction, orphaned roots, missing
edges, cycles, and disconnected subgraphs, then reports ready fronts (waves of
parallel work), estimated worker-sessions, and maximum parallelism. Run it
before handing an epic to a fleet.

---

## Workflows (formulas, molecules, gates)

Full treatment in `05-workflows.md`.

```bash
bd formula list [--type workflow|expansion|aspect|convoy]
bd formula show <name> [--json]
bd formula convert <name|path> [--all] [--delete] [--stdout]

bd cook <formula-file> [--var k=v] [--mode compile|runtime] [--dry-run]
                       [--persist] [--force] [--prefix p] [--search-path p]

bd mol pour <proto-id> [--var k=v] [--assignee a] [--attach proto]
                       [--attach-type sequential|parallel|conditional] [--dry-run]
bd mol wisp <proto-id> [--var k=v] [--root-only] [--dry-run]
bd mol wisp list [--all] [--type t]
bd mol wisp gc [--age 1h] [--all] [--closed] [--exclude-type a,b] [-f/--force] [--dry-run]
bd mol show <id> [-p/--parallel]
bd mol current [<id>] [--for agent] [--limit n] [--range 100-150]
bd mol progress [<id>]
bd mol ready --gated
bd mol stale [--blocking] [--unassigned] [--all]
bd mol last-activity <id>
bd mol bond <A> <B> [--type sequential|parallel|conditional] [--pour|--ephemeral]
                    [--ref 'arm-{{name}}'] [--var k=v] [--as title] [--dry-run]
bd mol squash <id> [--summary "..."] [--keep-children] [--dry-run]
bd mol burn <id...> [--force] [--dry-run]
bd mol distill <epic-id> [formula-name] [--var k=v] [--output dir] [--dry-run]
bd mol seed <formula-name> [--var k=v]

bd gate create --blocks <id> [-t human|timer|gh:run|gh:pr] [--await-id x]
               [--timeout 2h] [-r reason]
bd gate list [-a/--all] [-n/--limit]
bd gate show <gate-id>
bd gate check [-t gh|gh:run|gh:pr|timer|bead|all] [--dry-run] [-e/--escalate] [-l/--limit]
bd gate resolve <gate-id> [-r reason]
bd gate discover [-b branch] [-n dry-run] [-l limit] [-a max-age]
bd gate add-waiter <gate-id> <waiter>

bd merge-slot create|check|acquire|release [--holder <who>] [--wait]

bd todo [list [--all]] | add "<title>" [-p n] [-d desc] | done <id...> [--reason r]
bd promote <wisp-id> [-r reason]
bd defer <id...> [--until <when>] [--reason r]
bd undefer <id...>
```

---

## Memory, state, and human escalation

```bash
bd remember "<insight>" [--key <slug>]   # persistent memory, injected by bd prime
bd recall <key>
bd memories [<search>]
bd forget <key>

bd set-state <id> <dimension>=<value> [--reason r]
bd state <id> <dimension>
bd state list <id>

bd human                       # focused help for humans
bd human list [-s status]      # beads labelled 'human'
bd human respond <id> -r "<answer>"    # comments and closes
bd human dismiss <id> [--reason r]
bd human stats

bd kv set|get|clear|list       # small key/value store for flags and scratch state
```

`bd remember` stores the memory **content**; the key is auto-generated unless
you pass `--key`. As a convenience, `bd remember <existing-key>` recalls
instead of storing. Memories are excluded from `bd export` unless
`--include-memories` or `--all`.

---

## Sync, data, and version control

Detail in `08-sync-and-storage.md`.

```bash
bd dolt push [--force] [--remote <name>]
bd dolt pull [--remote <name>]
bd dolt commit [-m msg]
bd dolt remote add <name> <url> | list | remove <name>
bd dolt start | stop [--force] | status | show | test | killall | clean-databases
bd dolt set <key> <value> [--update-config]     # database|host|port|user|data-dir

bd bootstrap [--dry-run] [--json] [-y/--yes]
bd export [-o file] [--all] [--include-infra] [--include-memories] [--scrub]
bd import [file|-] [--dry-run] [--dedup] [--allow-stale] [-i file]
bd backup init <path> | sync | restore [path] [--force] | remove | status
bd restore <id> [--apply] [--json]      # un-compact a bead (different from backup restore)

bd branch [name]
bd vc status | commit [-m msg] [--stdin] | merge <branch> [--strategy ours|theirs]
bd history <id> [--limit n]
bd diff <from-ref> <to-ref>

bd federation add-peer <name> <endpoint> [--user u] [--password p]
bd federation list-peers
bd federation sync [--peer p] [--strategy ours|theirs]
bd federation status [--peer p]

bd repo add <path> | list | remove <path> | sync [--verbose]
bd ship <capability> [--force] [--dry-run]
```

---

## Health, maintenance, and diagnostics

Detail in `11-troubleshooting.md`.

```bash
bd doctor [path] [--fix] [--dry-run] [-y/--yes] [-i/--interactive] [--deep]
          [--server] [--agent] [--json] [--check artifacts|conventions|pollution|validate]
          [--clean] [--perf] [-o out.json] [--migration pre|post] [--fix-child-parent]
bd ping [--json]
bd where [--json]                  # which .beads is actually active
bd context [--json]                # backend identity without opening the DB
bd info [--json] [--schema] [--whats-new] [--thanks]
bd version
bd status | bd stats [--no-activity] [--assigned] [--json]

bd lint [<id...>] [-s status] [-t type]
bd preflight [--check] [--json] [--skip-lint]
bd orphans [--details] [-f/--fix] [-l label] [--label-any a,b]
bd recompute-blocked [--json]
bd graph check

bd prune --older-than 30d [--pattern glob] [-f/--force] [--dry-run]
bd purge [--older-than 7d] [--pattern glob] [-f/--force] [--dry-run]
bd compact [--days 30] [-f/--force] [--dry-run]     # squash old Dolt commits
bd flatten [-f/--force] [--dry-run]                 # squash ALL history (irreversible)
bd gc [--older-than 90] [--skip-decay] [--skip-dolt] [-f/--force] [--dry-run]
bd admin compact [--stats|--analyze|--apply|--auto|--dolt] [--id x] [--summary f]
bd admin cleanup [--older-than n] [--ephemeral] [--cascade] [-f/--force] [--dry-run]
bd admin reset [--force]

bd migrate [--inspect] [--dry-run] [--yes] [--json] [--update-repo-id]
bd migrate schema | hooks | issues | sync
bd upgrade status | review | ack
bd rename <old-id> <new-id>
bd rename-prefix <new-prefix> [--dry-run] [--repair]

bd hooks install [--beads] [--shared] [--chain] [--force] | list | uninstall | run <hook>
bd sql "<query>" [--csv] [--json]      # server mode only
bd batch [-f file] [-m msg] [--dry-run]
bd metrics [on|off|example]
bd audit record [...] | label <entry-id> --label good --reason "..."
```

---

## Setup, context, and help

```bash
bd prime [--hook-json] [--memories-only] [--full] [--mcp] [--stealth] [--export]
bd onboard                       # ~10-line snippet for an agent instructions file
bd setup [recipe] [--list] [--check] [--remove] [--global] [--project]
         [--stealth] [--print] [-o path] [--add name path]
bd quickstart
bd help [command] [--all] [--list] [--doc <cmd>] [--docs-root <dir>]
bd completion bash|zsh|fish|powershell
bd init-safety                   # the bd init flag safety contract
```

---

## External tracker integrations

All follow the same shape: `sync` (bidirectional by default), plus `pull` /
`push` for specific IDs, `status`, and a discovery subcommand. Configure via
`bd config set <tracker>.*` or environment variables; every tracker records
`<tracker>.last_sync` for incremental syncs. See `09-configuration.md`.

```bash
bd github sync [--pull-only|--push-only] [--issues a,b] [--parent id]
               [--prefer-newer|--prefer-local|--prefer-github] [--dry-run]
bd github pull <refs...> | push <ids...> | repos | status

bd gitlab sync [--pull-only|--push-only] [--label l] [--milestone m]
               [--type t] [--exclude-type t] [--project id] [--assignee a]
bd gitlab pull | push | projects | status

bd jira sync [--pull|--push] [--create-only] [--project k]
             [--prefer-local|--prefer-jira] [--state open|closed|all]
bd jira pull | push | status

bd linear sync [--pull|--push|--pull-if-stale] [--threshold 20m] [--team ids]
               [--relations] [--milestones] [--type t] [--exclude-type t]
               [--include-ephemeral] [--parent id] [--prefer-local|--prefer-linear]
bd linear pull | push | teams | status

bd ado sync [--pull-only|--push-only] [--area-path p] [--iteration-path p]
            [--types t] [--states s] [--no-create] [--reconcile] [--bootstrap-match]
bd ado pull | push | projects | status

bd notion init --parent <page-id> | connect --url <url>
bd notion sync [--pull|--push] [--create-only] [--issues a,b] [--parent id]
bd notion pull | push | status
```

Migration idiom: configure credentials, then run the sync in the pull
direction (`bd github sync --pull-only`, `bd jira sync --pull`,
`bd linear sync --pull`). Export back out with the push direction, or
`bd export -o issues.jsonl` plus a conversion script for anything else.

---

## Commands you should not reach for as an agent

| Command | Why |
|---|---|
| `bd edit` | Opens `$EDITOR`; blocks forever |
| `bd create-form` | Interactive TUI |
| `bd flatten` | Irreversibly destroys all Dolt history |
| `bd admin reset --force` | Deletes the local workspace |
| `bd init --force` / `--reinit-local` / `--discard-remote` | Destructive re-init; the safety refusals exist for a reason |
| `bd dolt push --force` | Overwrites the team's remote history |
| `bd sql` with DML | Bypasses the storage layer and is not journaled |
| `bd metrics on/off` | A user preference, not an agent decision |

Ask the user before running any of these.
