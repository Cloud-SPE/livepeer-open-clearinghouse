# 08 — Sync, storage, and data lifecycle

How issue data is stored, moved between machines, backed up, and reclaimed.

---

## 1. The model in one picture

```
        your machine                    git remote (origin)              teammate
   ┌──────────────────────┐          ┌────────────────────┐        ┌──────────────┐
   │ Dolt DB              │          │  refs/dolt/data    │        │  Dolt DB     │
   │ .beads/embeddeddolt/ │──push──▶ │  (issue history)   │ ◀────▶ │  (replica)   │
   │ (source of truth)    │◀──pull── │                    │        │              │
   └──────────────────────┘          └────────────────────┘        └──────────────┘
             │
             └── optional passive export → .beads/issues.jsonl  (viewers, interchange)
```

Beads stores everything in [Dolt](https://github.com/dolthub/dolt), a
version-controlled SQL database with **cell-level merge**. Every write
auto-commits to Dolt history. Sync is native Dolt push/pull, riding on your
existing git remote under `refs/dolt/data` — a separate ref namespace from
`refs/heads/`, so it never conflicts with protected branches and needs no
`beads-sync` branch.

**The three sentences that prevent most data loss:**

1. The Dolt database is the source of truth. Not the JSONL.
2. `.beads/issues.jsonl` is an optional export for viewers and interchange —
   not sync, not a backup, and unable to represent deletes.
3. Sync is `bd dolt push` / `bd dolt pull`. Backup is `bd backup`.

---

## 2. Day-to-day sync

```bash
bd dolt push          # publish local commits
bd dolt pull          # fetch and merge
bd dolt push --force  # overwrite remote history — ask a human first
bd dolt push --remote <name>
bd dolt commit -m "checkpoint"     # commit the working set explicitly
```

Typical two-machine rhythm:

```
Machine A                          Machine B
bd create "New task" -p 1
bd dolt push
                                   bd dolt pull
                                   bd update bd-a1b2 --claim
                                   bd close bd-a1b2 --reason "Done"
                                   bd dolt push
bd dolt pull
bd list                            # sees the closed task
```

Rules that are not optional:

- **Always use `bd dolt …`**, never the raw `dolt` CLI while the server is
  running — it causes journal corruption.
- **Commit before pulling.** Uncommitted working-set changes make
  `bd dolt pull` fail with "cannot merge with uncommitted changes"; run
  `bd dolt commit` first.
- **Push before switching machines.** Unpushed work exists only locally.
- **Never use JSONL as sync.** `bd import` is upsert-only; it cannot infer that
  records absent from an export were deleted or pruned.

---

## 3. Remotes

`bd init` auto-detects `git remote get-url origin` and configures a Dolt remote
named `origin`, persisted as `sync.remote` in `.beads/config.yaml`. Commit that
config so fresh clones can bootstrap.

```bash
bd dolt remote list
bd dolt remote add origin git+ssh://git@github.com/org/repo.git
bd dolt remote add origin git+https://github.com/org/repo.git
bd dolt remote add origin https://doltremoteapi.dolthub.com/org/beads
bd dolt remote add origin gs://bucket/path
bd dolt remote add origin aws://bucket/path      # or s3://
bd dolt remote add origin file:///path/to/remote
bd dolt remote remove origin
```

Always use `bd dolt remote add`, not raw `dolt remote add`: bd registers the
remote through the store API so a running SQL server sees it immediately.
Remotes added with the `dolt` CLI land in filesystem config only, and push/pull
then fail with *remote not found* until the server restarts. `bd doctor`
reports legacy CLI-only or mismatched remotes under "Dolt Remote Migration".

Verify the remote actually received data:

```bash
git ls-remote origin | grep dolt
# expect: <hash>  refs/dolt/data
```

For git-protocol remotes, credentialed external servers, and cloud remotes
whose credentials only exist in the current shell, `bd dolt push/pull`
materializes a matching local CLI remote and uses the `dolt` CLI transport —
that mirror is a transport detail, not a second configuration source.

Hosted Dolt auth: `DOLT_REMOTE_USER`, `DOLT_REMOTE_PASSWORD`.

---

## 4. Bootstrapping a clone

`git clone` does **not** fetch `refs/dolt/data`.

```bash
git clone git@github.com:org/repo.git && cd repo
bd bootstrap            # auto-detects, clones the DB, wires the remote
bd list && bd history   # verify
```

`bd bootstrap` never deletes issues. Its decision order: clone from
`sync.remote`; else clone from git origin's `refs/dolt/data`; else restore from
`.beads/backup/*.jsonl`; else import `.beads/issues.jsonl`; else create fresh;
else validate the existing database. `--dry-run`, `--json`, `-y/--yes`.

Retrofitting a remote onto an old project (run from the machine whose local
database is authoritative):

```bash
bd dolt remote list                                   # empty?
bd export -o .beads/issues.pre-remote.jsonl           # optional audit export
bd dolt remote add origin git+ssh://git@github.com/org/repo.git
bd dolt push
# commit the resulting .beads/config.yaml change
```

Then other machines run `bd dolt pull`, or `bd bootstrap` if their database is
missing or stale.

---

## 5. Auto-commit, auto-backup, auto-push

Three post-write behaviours, in this order after each successful write.

### Auto-commit

Two different "commits" exist: a **SQL transaction commit** (durable in the
Dolt *working set*) and a **Dolt version-control commit** (recorded in
*history*, visible to `bd history` and to push/pull/merge).

`dolt.auto-commit` controls the second. Embedded mode defaults to on — every
write command creates a history commit, which is why history grows and why
`bd compact` exists. Server mode defaults to off, because firing `DOLT_COMMIT`
after every write under concurrent load produces "database is read only"
errors; there, commit explicitly.

```bash
bd --dolt-auto-commit off create "No history commit for this one"
bd --dolt-auto-commit batch create "..."     # accumulate, then:
bd dolt commit -m "Batch: created issues"
```

### Auto-backup (opt-in)

```yaml
backup:
  enabled: true
  interval: 15m
```

After each write, bd compares the Dolt HEAD hash against the last backup state
and, if data changed and the throttle has elapsed, syncs a **Dolt-native**
backup to `.beads/backup/` (or a `backup/` directory inside `backup.git-repo`).
State lives in `backup_state.json`. Unlike a JSONL export this preserves
tables, branches, commit history, and working-set data.

### Auto-push (opt-in, deliberately)

```yaml
dolt:
  auto-push: true
  auto-push-interval: 5m
  auto-push-timeout: 30s
```

Off by default because concurrent pushes to git-protocol Dolt remotes can
corrupt or strand remote history when writers race. When on: debounced by
interval, skipped when HEAD has not changed, failures are warnings only, state
in the per-machine `.beads/push-state.json` (never in the database, to avoid
cross-machine merge conflicts). Before pushing, bd verifies the local chunk
store with `dolt fsck --quiet` under a 30s timeout — raise it for large stores
with `BEADS_FSCK_TIMEOUT=2m`.

---

## 6. Export and import (interchange, not sync)

```bash
bd export                                  # JSONL to stdout
bd export -o issues.jsonl
bd export --all -o full.jsonl              # infra + templates + gates + memories
bd export --include-memories
bd export --include-infra
bd export --scrub -o clean.jsonl           # drop test/pollution records

bd import backup.jsonl
bd import -                                # stdin
cat issues.jsonl | bd import -
bd import --dry-run
bd import --dedup                          # skip titles matching an open bead
bd import --allow-stale old.jsonl          # deliberately restore an older snapshot
bd import --json
```

Semantics that matter:

- Import is **upsert**: matching IDs update, new IDs create. Hash IDs are
  content-derived and stable, so a matching ID means the same bead.
- A row rewrites an existing bead only when its `updated_at` is **strictly
  newer**. Older rows are skipped (`stale_skipped_ids`); ties keep every local
  column (`tie_kept_local_ids`) because `updated_at` has second granularity.
  The guard is enforced inside the upsert, so a concurrent local write is
  preserved. `--allow-stale` overrides.
- `--json` reports `created`, `updated`, `unchanged`, `skipped`,
  `dedup_skipped`, `memories`, `ids`, `updated_issues` (field-level summary),
  `tie_kept_local_ids`, `stale_skipped_ids`, `skipped_dependencies`, `dry_run`.
- Memory records (`"_type":"memory"`) round-trip, so `bd export | bd import`
  carries both issues and memories.
- Only `title` is required per line; everything `bd export` emits is accepted.
- Rows with `status: "tombstone"` are skipped.

Enable the passive export only if something consumes it (a viewer, or
`bd repo sync` hydration):

```bash
bd config set export.auto true
bd config set export.git-add true      # stage it in the same commit
bd config set export.path issues.jsonl
bd config set export.interval 60s
```

---

## 7. Backups

```bash
bd backup init <path>          # filesystem dir OR DoltHub URL   (alias: add)
bd backup sync                 # push the whole database to it (atomic)
bd backup restore [path] --force
bd backup status
bd backup remove               # unregister (does not delete the backup data)
```

DoltHub destinations use `DOLT_REMOTE_USER` / `DOLT_REMOTE_PASSWORD`.

A `bd backup` is a full database backup: tables, branches, commit history,
working set. A `bd export` is issue records only. Use export for portability
and pre-migration safety nets; use backup for restorable snapshots.

### Migrating between storage modes

Both directions preserve full Dolt history. `bd export` is **not** a substitute.

```bash
# In the source project (either mode)
bd backup init /path/to/backup-dir
bd backup sync

# In a new project of the target mode
mkdir new-project && cd new-project
bd init            # or: bd init --server
bd backup restore --force /path/to/backup-dir
bd list && bd backup status
```

`--force` overwrites the freshly-initialized database. Restore also updates
`metadata.json` to the restored identity, registers the backup directory for
future syncs, and backfills the embedded migration tracker.

---

## 8. Dolt version control

Dolt keeps its own history, separate from git.

```bash
bd history <id> [--limit n]        # an issue's version history
bd diff <from-ref> <to-ref>        # e.g. bd diff HEAD~5 HEAD
bd branch                          # list Dolt branches
bd branch feature-x                # create one
bd vc status                       # current branch, hash, uncommitted changes
bd vc commit -m "checkpoint"
bd vc merge feature-x [--strategy ours|theirs]
bd show <id> --as-of <commit-or-branch>
```

Power-user access to the underlying store (embedded: `.beads/embeddeddolt/`,
server: `.beads/dolt/`) — only with every writer stopped:

```bash
cd .beads/embeddeddolt
dolt log
dolt diff main feature-x
dolt blame issues
dolt sql -q "SELECT * FROM issues"
```

---

## 9. Server lifecycle (server mode only)

```bash
bd dolt start | stop [--force] | status | show | test
bd dolt killall                 # kill orphan sql-servers for THIS project's data dir
bd dolt clean-databases [--dry-run]   # drop leftover testdb_*/beads_t* databases
bd dolt set database|host|port|user|data-dir <value> [--update-config]
```

The server auto-starts transparently when needed; explicit control is for
diagnostics. Runtime files live in `.beads/`: `dolt-server.pid`,
`dolt-server.log`, `dolt-server.port`.

Embedded mode has no server, no ports, and no log file — `bd dolt status`
reports the in-process engine and the on-disk data directory instead.

---

## 10. Git integration

`bd init` installs thin shim hooks that call `bd hooks run <name>`, so
upgrading `bd` updates hook behaviour automatically.

| Hook | What it does |
|---|---|
| `pre-commit` | Runs chained hooks; exports `.beads/issues.jsonl` when `export.auto` is on, so it lands in the same commit |
| `post-merge` | Runs chained hooks; imports JSONL **only** as a legacy fallback when no Dolt remote is configured |
| `pre-push` | Runs chained hooks |
| `post-checkout` | Runs chained hooks |
| `prepare-commit-msg` | Adds an `Executed-By:` agent identity trailer |

```bash
bd hooks install            # .git/hooks/
bd hooks install --beads    # .beads/hooks/  (recommended for the Dolt backend)
bd hooks install --shared   # .beads-hooks/  (versioned, shareable)
bd hooks install --chain    # run pre-existing hooks first
bd hooks list               # installed | outdated | missing
bd hooks uninstall
```

Shims use section markers, so content outside the markers survives installs and
upgrades. Installation is worktree-aware. **Re-run `bd hooks install` after
every `bd` upgrade.**

External hook managers are detected (lefthook, husky, pre-commit, prek, hk,
overcommit, yorkie, simple-git-hooks); `bd doctor` reports whether they call
`bd hooks run`, and `bd doctor --fix` reinstalls with `--chain`. For
config-driven managers, add a `bd hooks run <hook>` step directly.

Hook timeout: 300s default, override with `BEADS_HOOK_TIMEOUT` (positive whole
seconds). The deadline covers your *entire* chained pipeline, so slow
eslint/tsc chains need a larger value. On timeout beads warns and lets the git
operation proceed.

Everything except the identity trailer and hook chaining works with **no hooks
at all** — useful for `bd init --skip-hooks` and for branchless VCS like
Jujutsu. For pure `jj` repos:

```toml
# ~/.config/jj/config.toml
[aliases]
push = ["util", "exec", "--", "sh", "-c", "bd dolt commit && bd dolt push && jj git push \"$@\"", ""]
```

---

## 11. Worktrees

Beads works in git worktrees with no setup. All worktrees in a repository share
one `.beads` workspace — discovery follows `BEADS_DIR` if set, otherwise the
main repository's `.beads`.

```bash
git worktree add ../project-feature feature-branch
cd ../project-feature
bd ready && bd create "Implement X" -t feature -p 1
bd where                     # authoritative: which workspace is active

bd worktree create feature-auth [--branch fix-1]
bd worktree list
bd worktree info [--json]
bd worktree remove feature-auth [--force]
```

A local `./.beads` may legitimately be absent in a worktree — that is not a
bug. Embedded mode serves one writer at a time; for concurrent writers across
worktrees, use server mode.

An **external** workspace shared by many checkouts:

```bash
export BEADS_DIR=~/project-beads/.beads
cd ~/project/main && bd list
cd ~/project/feature-1 && bd list
```

With an external `BEADS_DIR`, push/pull target that workspace, not the code
repo.

Legacy cleanup (an old, removed sync-branch feature created hidden worktrees
that can lock branches):

```bash
rm -rf .git/beads-worktrees .git/worktrees/beads-*
git worktree prune
bd config set sync.branch ""
```

### Database redirects

Multiple clones can share one database via a one-line `.beads/redirect` file:

```bash
mkdir -p .beads && echo "../main-clone/.beads" > .beads/redirect
bd where --json
```

Redirect chains are not followed (one level only), the target must exist and
contain a valid database, and separate projects or long-lived forks should get
their own databases instead. Worktrees do not need redirects.

---

## 12. Reclaiming space

Four tools with different blast radii:

| Command | Removes | Reversible |
|---|---|---|
| `bd admin compact` | Text of old closed beads, replaced by a summary | `bd restore <id> --apply` |
| `bd prune --older-than 30d --force` | Closed **non-ephemeral** beads | no |
| `bd purge --force` | Closed **ephemeral** beads (wisps) | no |
| `bd compact` | Squashes Dolt commits older than N days | no |
| `bd flatten --force` | **All** Dolt history → one commit | no |
| `bd gc` | Decay + compact + Dolt GC in one pass | no |

```bash
bd status                              # counts first
du -sh .beads/embeddeddolt             # on-disk size

bd prune --older-than 30d              # preview
bd prune --older-than 30d --force
bd prune --older-than 90d --ignore-references --force   # override link protection
bd purge --force
bd compact --dry-run && bd compact --days 30 --force
bd gc --dry-run
bd gc --older-than 30
bd gc --skip-decay --skip-dolt
bd flatten --dry-run                   # last resort; irreversible
```

`bd prune` requires `--older-than` or `--pattern` (use `--pattern '*'` to mean
everything closed) and skips pinned, open, in-progress, and ephemeral beads. It
is reference-aware: closed beads whose ID appears in an open bead's
description, notes, or comments are protected, so ADRs and decision records
that live work still cites are not deleted.

Deleting rows frees rows, not disk — pair pruning with `bd flatten` or
`bd admin compact --dolt` (Dolt GC) to actually reclaim space.

`bd admin cleanup --force` deletes closed issues outright (`--older-than n`,
`--ephemeral`, `--cascade`) and is blunter than `bd prune`. `bd admin reset
--force` removes the entire local workspace.

---

## 13. Multi-clone hazards

- Stop the Dolt server (`bd dolt stop`) before switching between clones that
  share a data directory.
- Prefer embedded mode for automated workflows; prefer server mode for
  genuinely concurrent writers.
- `bd admin reset --force` only clears **local** data — old issues return from
  the remote or from other clones that push later.
- Never migrate two clones of one remote independently across a schema
  migration: exactly one designated migrator, everyone else `bd bootstrap`.
  See `11-troubleshooting.md` for the `pk-fork-refused` recovery.
- `sync.require_confirmation_on_mass_delete` prompts before pushing a merge
  that deletes most of your issues — worth enabling on shared databases.
