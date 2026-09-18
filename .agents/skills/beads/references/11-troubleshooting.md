# 11 — Troubleshooting and recovery

Diagnose first, then act. Most beads problems are one of five things: the wrong
workspace, a stale flag, a missing bootstrap, a safety refusal doing its job,
or a Dolt server that is not where you think it is.

---

## 1. First moves, in order

```bash
bd where            # WHICH .beads is actually active (redirects, BEADS_DIR)
bd doctor           # health: schema, migrations, hooks, gitignore, cycles
bd status           # counts, ready work, recent activity
bd ping             # can bd reach its database at all?
bd context --json   # backend identity WITHOUT opening the database
bd info --json      # database path, stats, schema
bd version
```

`bd context` is the one that still works when the database will not open.

Before any `--fix`:

```bash
cp -r .beads .beads.backup
bd doctor --dry-run      # what would change
bd doctor --fix          # or --fix -i to confirm each fix
```

`bd doctor --fix` may remove dependencies it flags as circular, **including
valid parent-child edges**. `--fix-child-parent` is opt-in for exactly that
reason. Read the dry-run output.

### `bd doctor` modes

```bash
bd doctor --agent [--json]      # observed vs expected state, remediation commands,
                                # source files, severity (blocking/degraded/advisory)
bd doctor --deep                # full graph integrity (slow on large DBs)
bd doctor --server              # server-mode connectivity, version, schema, pool
bd doctor --check artifacts [--clean]     # stale JSONL/SQLite/cruft dirs
bd doctor --check conventions             # lint + stale + orphans (advisory)
bd doctor --check pollution [--clean]     # test issues in the database
bd doctor --check validate [--fix]        # duplicates, orphaned deps, git conflicts
bd doctor --migration pre|post [--json]
bd doctor --perf                # timings + CPU profile for bug reports
bd doctor -o diagnostics.json   # export for historical comparison
```

`--agent` is the right mode for an AI agent: it explains rather than just
failing, and it is ZFC-compliant — Go observes and reports, the agent decides.

---

## 2. Installation problems

| Symptom | Fix |
|---|---|
| `bd: command not found` | Add `$(go env GOPATH)/bin` to `PATH`, or reinstall via Homebrew / install script |
| Wrong version running | `which -a bd`; delete the stale copy (classically `~/go/bin/bd` shadowing Homebrew), then `bd version` |
| `zsh: killed bd` on macOS | CGO build problem: reinstall with `CGO_ENABLED=1 GOFLAGS=-tags=gms_pure_go go install …`, or use Homebrew |
| Permission denied | `chmod +x $(which bd)` or install into `~/.local/bin` |
| Antivirus flags `bd` | Known false positive for Go binaries — verify SHA-256 against the release `checksums.txt`, then add an exclusion |
| macOS Gatekeeper block | Verify the checksum, then `xattr -d com.apple.quarantine /usr/local/bin/bd` |
| Windows: `bd init` hangs at 100% CPU | Controlled Folder Access is blocking it. Run `bd init -v` to see the real error, then whitelist `bd.exe` |
| Windows: firewall | Allow `bd.exe` loopback traffic (server mode listens on loopback TCP) |
| `bd federation` refuses | The binary was built without CGO; use a release binary |

---

## 3. Database and workspace problems

### Database not found

```bash
bd init --quiet                      # if it genuinely is a new project
BEADS_DIR=/path/to/.beads bd list    # if it exists elsewhere
bd where                             # what bd thinks it should use
```

### Multiple `.beads` directories detected

bd warns and marks the active one with `▶` (usually the closest to cwd).

- Nested projects: supported — just know which is active, or pin it.
- Accidental duplicates: `bd export -o issue-export.jsonl` from the unwanted
  one, then remove its `.beads`.
- Pin explicitly: `export BEADS_DIR=/path/to/.beads` (preferred over the
  deprecated `BEADS_DB`).

### Database locked

```bash
bd dolt stop            # server mode
ps aux | grep bd && kill <pid>
bd list
```

Embedded mode is single-writer by file lock; for real concurrency use server
mode. **Never** delete files inside `.dolt/` (including `noms/LOCK`) — Dolt
manages them and removing them causes unrecoverable corruption.

### `bd` shows 0 issues but the database has data (server mode)

bd is connected to a different server/database — an empty "shadow".

```bash
grep -E "dolt_mode|dolt_server_port" .beads/metadata.json
bd doctor --server
bd sql 'SELECT COUNT(*) FROM issues'
```

Fix: make sure the server runs from the correct data directory and
`metadata.json` points at the right server/port. A stale `.beads/dolt/`
alongside an external-server config can shadow the real database — confirm
where the real data lives before removing anything.

### Configured server unreachable

When `metadata.json` has an explicit `dolt_server_port`, bd treats the server
as externally managed and **disables auto-start** on purpose (spawning another
server would create a shadow database).

```bash
bd dolt start
# or
dolt sql-server --host 127.0.0.1 --port 3307 --data-dir /path/to/dolt/data
```

Remove `dolt_server_port` from `metadata.json` if you want auto-start back.

### Port conflicts across projects

```bash
export BEADS_DOLT_SHARED_SERVER=1      # machine-wide
bd config set dolt.shared-server true  # per project
bd init --reinit-local -q              # existing projects may need their DB created on the shared server
bd dolt status                         # verify: same server, port 3308, ~/.beads/shared-server/
```

### Circuit breaker: "server appears down, failing fast"

State lives in `/tmp/beads-circuit/beads-dolt-circuit-<host>-<port>[-<db>].json`
and is shared across all `bd` processes, so every command fails until a
successful probe resets it.

```bash
cat /tmp/beads-circuit/beads-dolt-circuit-*.json
lsof -i :<port>
grep port .beads/metadata.json
rm /tmp/beads-circuit/beads-dolt-circuit-*.json
bd dolt stop && bd dolt start && bd list
```

On macOS `/tmp` → `/private/tmp` is not always cleared on reboot, so the state
file can outlive a restart.

### Dolt journal corruption after an unclean shutdown

`.beads/dolt-server.log` contains
`possible data loss detected in journal file at offset …: corrupted journal`.
bd will not run Dolt's data-loss repair automatically.

If the remote is current:

```bash
mv .beads/dolt .beads/dolt.corrupt.$(date +%Y%m%dT%H%M%S)
bd bootstrap --dry-run
bd bootstrap --yes
bd stats
```

If the remote may be stale, keep the corrupt directory for forensics and
inspect it with `dolt fsck` before even considering
`dolt fsck --revive-journal-with-data-loss`.

### Physical corruption, general case

```bash
cp -r .beads .beads.backup
mv .beads/embeddeddolt .beads/embeddeddolt.backup   # or .beads/dolt in server mode
bd init
bd dolt pull            # or: bd backup restore [path] --force
```

Distinguish **logical** problems (ID collisions, wrong prefixes, orphaned deps
— `bd doctor --fix`) from **physical** corruption (disk failure, power loss,
two processes writing an embedded DB — restore from remote or backup).

### `failed to import: issue already exists`

```bash
bd export -o safety.jsonl          # first
rm -rf .beads/embeddeddolt         # or .beads/dolt
bd init --from-jsonl
```

### Imported children whose parent is gone

Import accepts orphans rather than failing, so the children still arrive.
Recreate the parent or close the orphans. Prevent it with `bd delete --cascade`
and by reviewing `bd children <parent>` before deleting.

### Old data comes back after a reset

`bd admin reset --force` removes **local** data only. Issues return from
configured remotes or from other clones that push afterwards. Reset every
clone, or clear the remote's beads data, before re-initializing.

---

## 4. `bd init` / `bd dolt` safety refusals

These are guards, not bugs. `bd help init-safety` prints the contract.

### `init-force-refused` (exit 10)

The remote already has Dolt history (`refs/dolt/data`) and you asked for local
history to win. Proceeding would create an orphan branch with no common
ancestor; the next push would fail or destroy the team's data.

```bash
bd bootstrap      # adopt the remote's history (what you almost always want)
bd doctor         # diagnose first, changes nothing
bd dolt status
```

To genuinely overwrite the remote (coordinate with the team first):
`bd init --reinit-local --discard-remote`, then a history-replacing push.

### `init-local-exists` (exit 11)

Local `.beads/` has issues a re-init would destroy.

```bash
bd export > issue-export.jsonl     # not a full backup, but issue-complete
bd init --reinit-local
```

Use `bd backup` instead when the database is healthy enough to produce a
restorable backup.

### `init-token-missing` (exit 12)

`--discard-remote` non-interactively without a token. Format is
`DESTROY-<issue-prefix>`:

```bash
bd init --reinit-local --discard-remote --destroy-token=DESTROY-bd
```

The token is deliberately **not** echoed in error messages, so automation must
template it from project state rather than scraping the error.

### `pk-fork-refused`

```
Error: … cannot merge because table dependencies has different primary keys
in its common ancestor
```

Two histories disagree about a table's **primary key set**. Dolt refuses before
row conflicts materialize, so `bd dolt pull`'s conflict resolver never runs.
**Retrying never helps** — the histories are permanently un-mergeable.

Cause: upgrading `bd` independently on two clones, with unsynced changes on
both sides, across a release whose migrations reshape a primary key.

Recovery — pick one canonical clone and re-clone the rest:

```bash
# 1. Compare candidates (read-only, on each clone)
bd stats
bd dolt status

# 2. On the canonical clone
bd version
bd doctor
bd dolt push --force        # make the remote authoritative

# 3. On EVERY other clone
bd export --all -o /tmp/beads-local.jsonl   # safety net for unsynced work
rm -rf .beads/dolt                          # discard the un-mergeable history
bd bootstrap                                # re-clone from the remote
bd import /tmp/beads-local.jsonl            # re-apply local-only work
bd stats                                    # spot-check
```

`bd import` upserts: local-only beads are recreated, newer local edits applied,
older rows skipped.

Prevention: sync every clone **before** upgrading (while all still run the old
binary), designate one migrator, and have every other clone `bd bootstrap`
rather than pull — `bd dolt pull` is refused while a clone has pending
migrations.

---

## 5. Sync problems

### Nothing syncs

```bash
bd dolt push
bd dolt remote list                 # is a remote even configured?
git ls-remote origin | grep dolt    # did refs/dolt/data ever land?
bd hooks list
bd doctor
```

### "cannot merge with uncommitted changes" on pull

```bash
bd dolt commit
bd dolt pull
```

### "no common ancestor" on push

A stale `refs/dolt/data` from a previous database:

```bash
git update-ref -d refs/dolt/data
bd dolt push
```

### "no store available" on push or commit

A bug in bd < 0.59.0. Upgrade.

### `bd list` shows nothing after a clone

The database was never bootstrapped: `bd bootstrap`. Manual fallback if
bootstrap cannot cope (old bd, unusual remote): confirm
`git ls-remote origin | grep dolt`, `bd init`, `bd dolt stop`, read
`dolt_database` from `.beads/metadata.json`, remove the empty
`.beads/dolt/<dbname>/`, `cd .beads/dolt && dolt clone <git-url> <dbname>`,
then `bd dolt start`, `bd migrate --yes`, and re-register the remote.

### Stale locks after a crash

```bash
bd doctor --fix --yes
```

Never remove files inside `.dolt/`.

### "fatal: Unable to read current working directory"

The server's working directory vanished (common after branch switches):

```bash
bd dolt stop && bd dolt start
```

### Merge conflicts

Dolt merges cell-by-cell, so concurrent changes conflict only when they touch
the same field of the same bead.

```bash
cp -r .beads .beads.backup
bd doctor
bd doctor --fix
bd list && bd stats
bd dolt push
```

If a pull leaves the store wedged (or, once `bd sync` ships, it exits 2),
inspect conflicts directly inside
`.beads/dolt/<db>`:

```bash
dolt sql -q 'select * from dolt_conflicts'    # positive check; do not trust exit codes alone
dolt conflicts cat issues
dolt conflicts resolve --theirs issues        # or --ours
dolt add -A && dolt commit -m 'resolve conflict' && dolt push origin main
```

### Sync failure runbook

```bash
bd dolt stop
ls -la .beads/*.lock && rm -f .beads/*.lock    # only when the server is definitely stopped
cp -r .beads .beads.backup
bd doctor --dry-run
bd doctor --fix
bd dolt start
bd dolt push && bd doctor
```

---

## 6. Dependency and ready-work problems

### `bd ready` is empty but there are open beads

```bash
bd blocked                    # the usual answer
bd list --status in_progress  # already claimed
bd list --deferred            # deferred out of the frontier
bd gate list                  # an open gate
bd dep tree <id> --max-depth 10
bd recompute-blocked          # stale is_blocked flags after a bad pull
```

`bd recompute-blocked` is idempotent, works in embedded **and** server mode
(unlike `bd doctor`, which is server-mode only for some checks), and repairs
the denormalized flag `bd ready` trusts. Reach for it when a pull was
interrupted or a conflict was resolved by hand.

Remember: only **blocking** dependency types gate ready work.

### Circular dependencies

```bash
bd dep cycles
bd graph check
bd blocked --verbose
bd show <a>; bd show <b>         # walk the chain
bd dep remove <dependent> <blocker>
bd blocked && bd ready           # verify
```

Prevention: "X needs Y", never "X before Y"; check `bd blocked` after wiring a
batch; keep chains shallow.

### Dependencies "not showing up"

```bash
bd show <id>          # human view includes deps
bd dep tree <id>
bd dep list <id> --direction up
```

---

## 7. Agent-specific problems

### Duplicate beads

```bash
bd search "<title>"                 # before creating
bd duplicates --dry-run
bd duplicates --auto-merge
bd duplicate <dup> --of <canonical>
bd find-duplicates --threshold 0.4
```

Label machine-created beads (`-l auto-generated`) so they are easy to audit.

### Agent confused by a complex graph

```bash
bd dep tree <id>
bd dep remove <dependent> <blocker>     # drop edges that are not real requirements
bd label add <id> related-to-feature-x  # use labels for loose association
```

### Sandboxed environments (Codex, Claude Code, containers)

Sandboxes that restrict process and network permissions can stop bd controlling
a Dolt server, producing persistent "database out of sync" errors or
`bd dolt stop` failing with "operation not permitted".

bd auto-detects sandboxes and prints `Sandbox detected, using direct mode`. If
detection fails, force it:

```bash
bd --sandbox ready
bd --sandbox create "Fix bug" -p 1
```

Sandbox mode disables Dolt auto-push so bd works without server control or
network access; sync manually once outside (`bd dolt push`). If staleness
errors persist, `bd doctor --fix` forces a metadata refresh (low risk — it
updates tracking metadata, not issues).

### MCP server not working

```bash
which beads-mcp
pip list | grep beads-mcp
which uv                       # the usual culprit
bd version && bd ready         # does the CLI itself work?
```

Then restart the client and check its MCP panel.

### Hooks not running

```bash
ls -la .git/hooks/
bd hooks install
bd hooks list
chmod +x .git/hooks/pre-commit
```

### Chained pre-commit hooks stopped running

`beads: hook 'pre-commit' timed out after 300s -- continuing without beads`.
The beads shim wraps `bd hooks run`, which chains to *your* hooks, so the
deadline covers the whole pipeline.

```bash
export BEADS_HOOK_TIMEOUT=600     # positive whole seconds
```

Beads uses GNU `timeout`/`gtimeout` only after a successful identity probe
(Windows `timeout.exe` is incompatible); otherwise a Perl `SIGALRM` fallback,
and if neither exists it warns and runs with no deadline. On timeout the git
operation proceeds anyway. After upgrading from a version with the name-only
timeout check, run `bd hooks install` once to refresh the hook sections.

### Corrupted symlinked `CLAUDE.md`

Git reports mode `120000` but the blob contains markdown (older setup bug):

```bash
git ls-files -s CLAUDE.md
sha=$(git rev-parse :CLAUDE.md)
git update-index --cacheinfo 100644,$sha,CLAUDE.md
git checkout-index -f -- CLAUDE.md
git ls-files -s CLAUDE.md        # now 100644
```

### "Branch already checked out" / stray `.git/beads-worktrees/`

Leftovers from a removed sync-branch feature:

```bash
rm -rf .git/beads-worktrees .git/worktrees/beads-*
git worktree prune
```

---

## 8. Performance

```bash
bd status
du -sh .beads/embeddeddolt         # or .beads/dolt
bd admin compact --dry-run --all
bd admin compact --analyze
bd admin compact --dolt            # Dolt GC to reclaim disk
bd doctor --perf                   # timings + profile
bd list --skip-labels              # skip label hydration when you don't need it
bd list -n 20                      # cap results
bd count --by-status               # counts without materializing rows
```

Beads is comfortable into the thousands of issues. Beyond ~100k, split by
component (`bd init --prefix fe` / `--prefix be`) or run `bd gc` regularly.

---

## 9. Debug environment variables

| Variable | Subsystem | Output |
|---|---|---|
| `BD_DEBUG` | General | stderr |
| `BD_DEBUG_RPC` | CLI ↔ Dolt server RPC | stderr |
| `BD_DEBUG_SYNC` | Sync and import timestamp protection | stderr |
| `BD_DEBUG_ROUTING` | Issue routing / multi-repo resolution | stderr |
| `BD_DEBUG_FRESHNESS` | Database file replacement detection | server log |

```bash
BD_DEBUG=1 bd ready
BD_DEBUG=1 bd dolt push 2> debug.log
BD_DEBUG_SYNC=1 bd dolt push        # "Protected bd-123: local=… >= incoming=…"
bd --verbose list
```

---

## 10. Filing a bug

```bash
bd version
bd info --json
bd doctor --agent --json
uname -a
```

Report at <https://github.com/gastownhall/beads/issues>, or ask in
GitHub Discussions. Include reproduction steps.

---

## 11. Quick reference: which command fixes what

| Problem | Command |
|---|---|
| Wrong workspace | `bd where`, `BEADS_DIR=…` |
| Stale ready/blocked state | `bd recompute-blocked` |
| Fresh clone has no data | `bd bootstrap` |
| Hooks stale after upgrade | `bd hooks install` |
| Config vs reality drift | `bd config drift`, `bd config apply` |
| Cycles | `bd dep cycles`, `bd dep remove` |
| Duplicates | `bd duplicates --auto-merge` |
| Completed epics still open | `bd epic close-eligible` |
| Complete-but-open molecules | `bd mol stale` |
| Beads referenced in commits but open | `bd orphans --fix` |
| Missing acceptance criteria | `bd lint` |
| Database too big | `bd gc`, `bd prune`, `bd purge`, `bd flatten` |
| Broken beyond repair, remote is good | `mv` the data dir aside, `bd init`, `bd dolt pull` |
