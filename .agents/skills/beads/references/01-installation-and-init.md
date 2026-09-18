# 01 — Installation, initialization, and lifecycle

Everything needed to get `bd` onto a machine, into a repository, and kept
current. Read `10-harness-integration.md` next if the goal is to wire an agent
harness to it.

---

## 1. What actually gets installed

Beads is installed **system-wide, once**. You never clone the beads repository
into a project. A project only ever contains a `.beads/` directory.

| Component | What it is | When you need it |
|---|---|---|
| `bd` CLI | The whole product, a single static Go binary with Dolt embedded | Always |
| Claude Code plugin | Upstream's optional `/beads:*` slash commands + hooks | Optional; conflicts with this skill's hook advice — see §7 |
| `beads-mcp` | Python MCP server that shells out to `bd` | Only where there is no shell (Claude Desktop, Amp) |

No runtime dependencies: no PostgreSQL, Redis, Docker, or node_modules. The
standalone `dolt` CLI is needed **only** for server mode or for direct database
surgery.

---

## 2. Installing the CLI

Pick one method and stay on it — mixed installs are the #1 cause of "wrong
version of bd running".

```bash
# macOS / Linux — recommended
brew install beads

# mise (macOS/Linux/Windows), tracks the latest GitHub release
mise install github:gastownhall/beads
mise use -g github:gastownhall/beads

# Install script (macOS/Linux/FreeBSD) — verifies release checksums
curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash

# Windows (PowerShell) — installs prebuilt release, verifies ZIP checksum
irm https://raw.githubusercontent.com/gastownhall/beads/main/install.ps1 | iex

# Node ecosystem
npm install -g @beads/bd
bun install -g --trust @beads/bd

# Arch Linux (community-maintained)
yay -S beads-git
```

### `go install` has two modes, and they are not equivalent

The module path is still `github.com/steveyegge/beads` even though the repo
moved to `gastownhall/beads`.

```bash
# Server-mode ONLY — no C compiler needed, but NO embedded Dolt.
# You must run an external `dolt sql-server` and use `bd init --server`.
CGO_ENABLED=0 go install github.com/steveyegge/beads/cmd/bd@latest

# Embedded-capable — needs a C compiler (gcc/clang, MinGW on Windows).
CGO_ENABLED=1 GOFLAGS=-tags=gms_pure_go go install github.com/steveyegge/beads/cmd/bd@latest
```

ICU headers are **not** required; `gms_pure_go` selects Go's stdlib regexp.
If you do not specifically need `go install`, use Homebrew or the install
script — both give you the embedded-capable build.

`bd federation …` additionally requires a CGO build; a `CGO_ENABLED=0` binary
prints an explanatory error instead of federating.

### Verify

```bash
bd version          # e.g. "bd version 1.2.2 (6c124203e: HEAD@6c124203e771)"
bd help
which -a bd         # MUST show exactly one path
```

If `which -a bd` shows several (classically a stale `~/go/bin/bd` shadowing
Homebrew), delete the extras. `bd: command not found` after `go install` means
`$(go env GOPATH)/bin` is not on `PATH`.

macOS Gatekeeper: verify the checksum against the release `checksums.txt`, then
`xattr -d com.apple.quarantine $(which bd)`. Windows Defender/Kaspersky
sometimes flag Go binaries as generic trojans — a known false positive; verify
the SHA-256 before adding an exclusion.

---

## 3. Initializing a project

```bash
cd your-project
bd init            # interactive: prompts for contributor/maintainer role
bd init --quiet    # AGENTS: non-interactive, installs hooks, no prompts
```

`bd init` does all of this:

1. Creates `.beads/` and an embedded Dolt database at `.beads/embeddeddolt/`.
2. Writes `.beads/.gitignore` so the database and runtime files never enter git.
3. Writes `.beads/metadata.json` (backend identity — **tracked in git**).
4. Installs git hooks (skip with `--skip-hooks`).
5. Creates or updates `AGENTS.md` and runs the project Codex/Claude setup
   (skip with `--skip-agents`, or go fully invisible with `--stealth`).
6. If the repo has a git `origin`, configures a Dolt remote named `origin`
   pointing at the same URL, persisted as `sync.remote` in `.beads/config.yaml`.
7. Prompts for your role, which sets `git config beads.role`.

The issue prefix defaults to the current directory name; override with
`--prefix`. Prefixes are lowercase letters/digits/hyphens, must start with a
letter, max 8 characters. The prefix **cannot** be changed with
`bd config set` — only `bd init --prefix`, `bd bootstrap`, or
`bd rename-prefix`.

### The `bd init` flags worth knowing

| Flag | Effect |
|---|---|
| `-p, --prefix <s>` | Issue prefix (default: directory name) |
| `-q, --quiet` | Suppress output |
| `--non-interactive` | Skip every prompt; role defaults to maintainer. Auto-detected when stdin is not a TTY or `CI=true` |
| `--skip-hooks` | Do not install git hooks |
| `--skip-agents` | Do not touch `AGENTS.md` or install Claude/Codex integration |
| `--stealth` | Invisible mode: `.git/info/exclude` entries, no hooks, no agent files |
| `--role maintainer\|contributor` | Set the role without prompting |
| `--contributor` | Run the OSS-contributor wizard (separate planning repo) |
| `--team` | Run the team wizard |
| `--server` | Use an external `dolt sql-server` instead of embedded |
| `--server-host/--server-port/--server-user/--server-socket` | Server connection details (password via `BEADS_DOLT_PASSWORD`) |
| `--shared-server` | One Dolt server at `~/.beads/shared-server/` for all projects |
| `--external` | The server is managed elsewhere; do not try to start it |
| `--remote <url>` | Clone the Dolt database from this remote and persist `sync.remote` |
| `--from-jsonl` | Seed from the configured `import.path` JSONL |
| `--database <name>` | Attach to an existing server database name |
| `--init-if-missing` | Idempotent: exit 0 if already initialized (good for scaffolds) |
| `--setup-exclude` | Add beads paths to `.git/info/exclude` (forks) |
| `--agents-file/--agents-profile/--agents-template` | Control the generated agent instructions |
| `--reinit-local` | Re-init over existing local data (destructive; see below) |
| `--discard-remote` + `--destroy-token` | Also discard the remote's history (very destructive) |

### Non-interactive init for agents

```bash
bd init --quiet --prefix myproj
# or, when you must not touch tracked files:
bd init --quiet --prefix myproj --skip-agents
```

When experimenting, **never init inside a real workspace**. Use a temp dir:

```bash
d=$(mktemp -d); ( cd "$d" && bd init --quiet --prefix test --skip-hooks --skip-agents && bd create "probe" ); rm -rf -- "$d"
```

`BEADS_DB` alone does not redirect `bd init`'s workspace setup.

### `bd init` refusals are a safety feature

`bd init` and `bd dolt` refuse operations that could destroy history, printing
a pattern code. Do not brute-force past them; see `11-troubleshooting.md`.

| Code | Exit | Meaning |
|---|---|---|
| `init-force-refused` | 10 | Remote already has Dolt history; you asked for local history to win. Use `bd bootstrap` to adopt the remote instead. |
| `init-local-exists` | 11 | Local `.beads/` has issues that a re-init would destroy. Export first. |
| `init-token-missing` | 12 | `--discard-remote` non-interactively without `--destroy-token=DESTROY-<prefix>`. |

`bd help init-safety` prints the full contract.

---

## 4. Joining a project that already uses beads

A plain `git clone` does **not** fetch `refs/dolt/data`, so the database is
absent. Do not run `bd init`:

```bash
git clone git@github.com:org/repo.git && cd repo
bd bootstrap          # auto-detects and clones the Dolt DB, wires the remote
bd list               # should show issues
bd doctor
```

`bd bootstrap` is non-destructive and never deletes issues. It picks its action
automatically: clone from `sync.remote`; else clone from git origin's
`refs/dolt/data`; else restore from `.beads/backup/*.jsonl`; else import
`.beads/issues.jsonl`; else create a fresh database; else validate the existing
one. Flags: `--dry-run`, `--json`, `-y/--yes`, `--non-interactive`.

---

## 5. Storage modes

| | Embedded (default) | Server |
|---|---|---|
| Command | `bd init` | `bd init --server` |
| Data | `.beads/embeddeddolt/` | `.beads/dolt/` |
| Writers | one at a time (file lock) | many concurrent |
| Process | in-process, no ports, no PID | external `dolt sql-server` |
| Needs `dolt` installed | no | yes |
| Good for | solo work, CI, containers, single agent | several agents on one machine, orchestrators, federation |

The choice is persisted in `.beads/metadata.json`. Migrate between modes with
`bd backup` (see `08-sync-and-storage.md`) — not with `bd export`.

Shared-server mode (`bd init --shared-server` or `BEADS_DOLT_SHARED_SERVER=1`)
runs one server at `~/.beads/shared-server/` on port 3308 for every project.
Each project **must** have a unique prefix, since the prefix is the database
name; identical prefixes would share one database, and bd's identity check
refuses to connect rather than corrupt data.

Server connection settings:

| Flag | Env | Default |
|---|---|---|
| `--server-host` | `BEADS_DOLT_SERVER_HOST` | `127.0.0.1` |
| `--server-port` | `BEADS_DOLT_SERVER_PORT` | `3307` (3308 shared) |
| `--server-socket` | `BEADS_DOLT_SERVER_SOCKET` | none (TCP) |
| `--server-user` | `BEADS_DOLT_SERVER_USER` | `root` |
| — | `BEADS_DOLT_PASSWORD` | none |

Unix sockets avoid port collisions and suit sandboxed environments; auto-start
is not supported in socket mode.

---

## 6. Directory layout

```text
.beads/
├── embeddeddolt/     # Dolt database, embedded mode (default) — gitignored
├── dolt/             # Dolt database, server mode              — gitignored
├── dolt-server.pid   # server-mode runtime (.pid/.log/.port)   — gitignored
├── push-state.json   # per-machine auto-push bookkeeping        — gitignored
├── issues.jsonl      # OPTIONAL passive export for viewers/interchange
├── metadata.json     # backend identity/config — TRACKED in git
├── config.yaml       # project config          — TRACKED in git
├── config.local.yaml # machine-local overrides — do not commit
├── formulas/         # workflow templates (see 05-workflows.md)
└── hooks/            # optional script hooks / bd-managed git hooks
```

Never track a database directory in git or Git LFS. If one was committed:
`bd doctor --fix` (updates the gitignore), `git rm --cached -r .beads/embeddeddolt/`,
commit, and use BFG or `git filter-repo` to purge history if needed.

---

## 7. First-run checklist for a fresh project

```bash
bd init --quiet --prefix <prefix>
bd doctor                     # health: hooks, schema, gitignore, migrations
bd hooks list                 # installed / outdated / missing
bd setup claude --check       # or codex/cursor/... — see 10-harness-integration.md
bd prime                      # confirm the workflow contract renders
bd create "Verify beads works" -p 3 -t chore -d "Smoke test." && bd ready
bd dolt remote list           # empty is fine locally; needed for team sync
```

Commit `.beads/metadata.json`, `.beads/config.yaml`, and whatever agent
instruction files you want shared (`AGENTS.md`, `CLAUDE.md`).

**If upstream's Claude Code plugin is installed** (`/plugin install beads`), it
registers its own SessionStart and PreCompact hooks running `bd prime`, and
`bd setup claude` will deliberately skip writing hooks so `bd prime` does not
fire twice. This skill assumes the plugin is *not* installed. If you want both,
pick one hook owner: either uninstall the plugin, or skip `bd setup claude`.

---

## 8. Upgrading

Replacing the binary is not the whole story. Order matters, because once the
new binary is installed a database with pending migrations is gated on **every**
open — including `bd dolt push` and `bd dolt pull`.

```bash
# 1. With the CURRENT binary: publish and sync everything.
bd dolt push
bd dolt pull

# 2. Back up. Cheap, issue-complete, importable by any version.
bd export --all -o .beads/backup/pre-migrate-$(date +%Y%m%d).jsonl

# 3. Install the new binary (same method you used originally).

# 4. Post-upgrade housekeeping.
bd info --whats-new
bd hooks install
bd version
```

### Crossing a schema migration on a remote-backed database

Exactly one clone migrates. The others adopt.

```bash
# Designated migrator only:
bd migrate
bd dolt push

# Every other clone (after installing the new binary):
bd bootstrap        # re-clones the migrated DB; bd dolt pull is refused here
```

`bd bootstrap` replaces the local database, which is why step 1 published
everything first. Migrating two clones independently forks the schema and
produces `pk-fork-refused` — unrecoverable by merge; see `11-troubleshooting.md`.

The gate is state-aware: it auto-migrates when the remote is at the same schema
version (safe first-mover), stops and points at `bd bootstrap` when the remote
is already migrated, and stops for a human on a genuine fork.
`BD_SMART_GATE=0` makes it block unconditionally;
`BD_ALLOW_REMOTE_MIGRATE=1 bd migrate` declares this clone the designated
migrator for scripted upgrades — wire it into exactly one clone's job, never all.

`bd doctor` includes a migration-content-skew check that flags a forked schema.

### Migration commands

```bash
bd migrate --inspect --json   # plan + database state, for agent analysis
bd migrate --dry-run          # preview
bd migrate                    # apply
bd migrate --yes              # apply and clean up old files
bd migrate schema [--json]    # explicit, idempotent schema migration
bd upgrade status|review|ack  # what changed since the version you last ran
```

---

## 9. Coexisting with the files `bd init` generates

`bd init` does not only create `.beads/` — by default it also installs agent
instructions, and one of those is **a skill named `beads`**. Verified on 1.2.2,
a default `bd init --quiet` in a git repo writes:

```
.beads/…                          the database and hooks
.gitignore
AGENTS.md                         managed beads section
CLAUDE.md                         managed beads section
.claude/settings.json             SessionStart hook → bd prime
.codex/config.toml                [features] hooks = true
.codex/hooks.json                 SessionStart/PreCompact/PostCompact/UserPromptSubmit
.agents/skills/beads/SKILL.md     ← upstream's own ~2 KB beads skill
.agents/skills/beads/agents/openai.yaml
```

### What actually collides

| Harness | Skill location | Collision with this skill? |
|---|---|---|
| **Claude Code** | `.claude/skills/` or `~/.claude/skills/` | **No.** `bd init` writes a *hook* to `.claude/settings.json`, never a skill. |
| **Codex** | `.agents/skills/` | **Yes.** `bd init` writes `.agents/skills/beads/SKILL.md` — the same name. |

### Install order does not protect you

Installing this skill first and then running `bd init` does **not** work.
Verified: `bd init` overwrites an existing `.agents/skills/beads/SKILL.md`
silently — no prompt, no backup, no marker check. Ordering is the wrong lever;
the flag is the right one.

### Recommended setups

**Claude Code — nothing to do.** Install this skill at user scope
(`~/.claude/skills/beads/`) or project scope (`.claude/skills/beads/`) and run
`bd init` normally. `bd init` never touches either path. User scope is the
safer default: it is outside the repo entirely, so no beads command and no
teammate's `bd init` can reach it.

**Codex — skip the generated agent files, then restore the hooks.**

```bash
bd init --quiet --prefix <prefix> --skip-agents      # .beads/ + .gitignore ONLY
mkdir -p .agents/skills
cp -r /path/to/beads-skill/skills/beads .agents/skills/
```

`--skip-agents` also suppresses `.codex/hooks.json`, so you lose the `bd prime`
injection. Add it back by hand — the exact file is in
`10-harness-integration.md` §4 — along with:

```toml
# .codex/config.toml
[features]
hooks = true
```

Note that a later `bd setup codex` (or a plain `bd init` re-run) will clobber
the skill again. Keep your copy in version control and re-copy after any beads
setup command.

**Already initialized?** Remove upstream's generated skill first — verified to
remove `.agents/` cleanly and its managed `AGENTS.md` section, leaving `bd`
fully functional:

```bash
bd setup codex --remove
cp -r /path/to/beads-skill/skills/beads .agents/skills/
```

### Verifying you have the right skill

```bash
head -3 .agents/skills/beads/SKILL.md
```

Upstream's stub opens with `description: Use when working in a repository that
uses bd or Beads for durable project task tracking…`. If you see that instead
of your own description, it was clobbered — re-copy.

`bd` itself is unaffected either way: `AGENTS.md`, `CLAUDE.md`, and the skill
files are pure instructions. The database, hooks, and every command work
identically with none of them present.

---

## 10. Uninstalling / removing beads

```bash
bd admin reset            # dry-run: shows what would be removed
bd admin reset --force    # removes .beads/, bd git hooks, sync worktrees
```

`bd admin reset` removes **local** data only. Issues can come back from a
configured Dolt remote or from other clones that push afterwards. For a true
clean slate, reset every clone (or clear the remote's beads data) first. If an
old version left hidden sync worktrees:

```bash
rm -rf .git/beads-worktrees .git/worktrees/beads-*
git worktree prune
```

To remove the tool itself, uninstall by whatever method installed it
(`brew uninstall beads`, `npm uninstall -g @beads/bd`, `rm ~/go/bin/bd`, …).
