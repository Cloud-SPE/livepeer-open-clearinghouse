# 09 — Configuration

Where settings live, which wins, and the keys worth setting.

---

## 1. Two systems

| System | Stored in | For |
|---|---|---|
| **Tool-level** (Viper/YAML) | `config.yaml` files | Startup flags and CLI behaviour: output format, auto-commit, routing, sync, validation. User/machine preferences. |
| **Project-level** | The Dolt database | Integration settings, status/type maps, ID tuning. Team-shared; travels with `bd dolt push`. |

`bd config set` routes each key automatically. `beads.role` is special: it goes
into **git config**.

Secrets are refused in the database — database config is pushed to remotes,
which would expose them and trip secret scanning.

---

## 2. File locations and precedence

`config.yaml` is searched in this order, later files overriding earlier:

1. `~/.beads/config.yaml` (legacy user-level, lowest)
2. `~/.config/bd/config.yaml` (user-level; this exact path is checked on every platform)
3. `<repo>/.beads/config.yaml` (project-level, walked up from cwd)
4. `$BEADS_DIR/config.yaml` (highest, when `BEADS_DIR` points elsewhere)

`.beads/config.local.yaml` is merged in last, for machine-specific overrides
that should not be committed.

For YAML keys, precedence is:

1. command-line flags (`--json`, `--db`, `--actor`, …)
2. environment variables (`BD_*`, plus a few legacy `BEADS_*`)
3. `config.yaml` files, in the order above
4. built-in defaults

Database-stored keys are read at command time and have **no env override**.
When a YAML value or env var shadows a database key, `bd config list` prints a
warning and `bd config show` reports each key's source.

---

## 3. Managing configuration

```bash
bd config set <key> <value>
bd config set-many k1=v1 k2=v2        # one auto-commit, validated before writing
bd config get <key> [--json]
bd config list                        # database-stored config + override warnings
bd config show [--json] [--source config.yaml|env|default|metadata|database|git]
bd config validate                    # sync/routing/federation sanity
bd config unset <key>
bd config drift                       # read-only: hooks/remote/server vs config (exit 1 = drift)
bd config apply [--dry-run]           # reconcile reality to config
```

`bd config show` is the source of truth for "what is actually in effect here".
Unrecognized keys produce a did-you-mean warning; use the `custom.*` namespace
for your own keys.

---

## 4. YAML-only keys (read before the database opens)

Namespaces routed to YAML: `routing.*`, `sync.*`, `git.*`, `directory.*`,
`repos.*`, `external_projects.*`, `validation.*`, `hierarchy.*`, `ai.*`,
`backup.*`, `export.*`, `dolt.*`, `federation.*`, `metrics.*`, `list.*`.

Plus: `no-db`, `json`, `db`, `actor`, `identity`, `no-push`, `no-git-ops`,
`agent.profile`, `create.require-description`, `import.auto`, `import.path`,
`prime.max-memories`, `prime.max-memory-chars`, and the secret keys
`github.token`, `gitlab.token`, `jira.api_token`, `ado.pat`, `linear.api_key`,
`linear.oauth_client_id`, `linear.oauth_client_secret`.

Any key containing `api_key`, `api-key`, `secret`, `token`, or `password` is
treated as a secret and **refused on a git-tracked `config.yaml`** unless you
pass `--force-git-tracked`. Prefer environment variables.

---

## 5. Tool-level settings

| Key | Flag | Env | Default | Meaning |
|---|---|---|---|---|
| `json` | `--json` | `BD_JSON` | false | JSON output everywhere |
| `db` | `--db` | `BD_DB` | auto-discover | Database path |
| `actor` | `--actor` | `BEADS_ACTOR` | git `user.name` | Audit-trail actor |
| `identity` | `--identity` | `BEADS_IDENTITY` | git user / hostname | Sender identity for `bd mail` |
| `no-push` | `--no-push` | `BD_NO_PUSH` | false | Skip pushing in `bd dolt push` |
| `no-git-ops` | — | — | false | No git commands in the `bd prime` close protocol |
| `agent.profile` | — | `BD_AGENT_PROFILE` | `conservative` | Agent git authority (see §6) |
| `prime.max-memories` | `--max-memories` | `BD_PRIME_MAX_MEMORIES` | 0 (unlimited) | Cap memories injected by `bd prime` |
| `prime.max-memory-chars` | `--max-memory-chars` | `BD_PRIME_MAX_MEMORY_CHARS` | 0 | Byte cap, at whole-memory boundaries |
| `dolt.auto-commit` | `--dolt-auto-commit` | `BD_DOLT_AUTO_COMMIT` | on (embedded) / off (server) | Dolt history commit per write |
| `dolt.auto-push` | — | `BD_DOLT_AUTO_PUSH` | false | Auto-push after writes (opt-in) |
| `dolt.auto-push-interval` | — | `BD_DOLT_AUTO_PUSH_INTERVAL` | 5m | Debounce |
| `dolt.auto-push-timeout` | — | `BD_DOLT_AUTO_PUSH_TIMEOUT` | 30s | Bound one attempt |
| `dolt.shared-server` | `--shared-server` | `BEADS_DOLT_SHARED_SERVER` | false | One server for all projects |
| `dolt.max-conns` | — | `BEADS_DOLT_MAX_CONNS` | 10 | Pool size |
| `dolt.local-only` | — | — | false | Skip wiring a sync remote during `bd init` |
| `dolt.debug` | `--debug` | — | false | Run the managed server with debug logging + CPU profiling |
| `git.author` | — | `BD_GIT_AUTHOR` | none | Commit author for beads commits |
| `git.no-gpg-sign` | — | `BD_GIT_NO_GPG_SIGN` | false | Disable GPG signing |
| `create.require-description` | — | `BD_CREATE_REQUIRE_DESCRIPTION` | false | Reject descriptionless beads |
| `validation.on-create` | — | `BD_VALIDATION_ON_CREATE` | none | `none`/`warn`/`error` |
| `validation.on-close` | — | `BD_VALIDATION_ON_CLOSE` | none | Same |
| `validation.on-sync` | — | `BD_VALIDATION_ON_SYNC` | none | Same |
| `validation.metadata.mode` | — | — | none | Metadata schema validation |
| `hierarchy.max-depth` | — | — | 3 | Hierarchical ID nesting |
| `backup.enabled` | — | `BD_BACKUP_ENABLED` | false | Periodic Dolt-native backup |
| `backup.interval` | — | `BD_BACKUP_INTERVAL` | 15m | Throttle |
| `backup.git-repo` | — | `BD_BACKUP_GIT_REPO` | none | Backup into this repo's `backup/` |
| `backup.git-push` | — | — | false | Auto-push the backup repo |
| `export.auto` | — | — | false | Refresh `.beads/issues.jsonl` after writes |
| `export.path` | — | — | `issues.jsonl` | Relative to `.beads/` |
| `export.interval` | — | — | 60s | Throttle |
| `export.git-add` | — | — | false | `git add` the export |
| `import.auto` | — | `BD_IMPORT_AUTO` | true | Master switch for implicit JSONL imports (hook fallback + empty-DB recovery) |
| `import.path` | — | — | `issues.jsonl` | Relative to `.beads/` |
| `list.limit` | `-n/--limit` | `BD_LIST_LIMIT` | 50 | Default `bd list` cap |
| `routing.*` | — | — | see 07 | Multi-repo routing |
| `repos.primary` / `repos.additional` | — | — | unset | Hydration sources |
| `directory.labels` | — | — | {} | Map directory patterns → labels (monorepos) |
| `external_projects` | — | — | {} | Project name → path, for `external:` deps |
| `federation.remote` | — | `BD_FEDERATION_REMOTE` | none | Dolt remote URL |
| `federation.sovereignty` | — | `BD_FEDERATION_SOVEREIGNTY` | none | `T1`–`T4` |
| `federation.allowed-remote-patterns` | — | — | [] | Globs restricting remote URLs |
| `federation.exclude_types` | — | — | `[wisp]` | Types excluded from federation push |
| `sync.require_confirmation_on_mass_delete` | — | — | false | Prompt before pushing a mostly-deleting merge |
| `output.title-length` | — | — | 255 | Title width in feedback (0 hides) |
| `agents.file` | — | — | `AGENTS.md` | Agent instructions filename |
| `ai.model` | — | `BD_AI_MODEL` | `claude-haiku-4-5-20251001` | Model for AI-assisted features |
| `events-journal*` | — | `BD_EVENTS_JOURNAL*` | off / 7d / 100000 / auto | Ships with `bd events`, which is **not in 1.2.2** — see `12-json-and-scripting.md` |
| `node_id` | — | `BEADS_NODE_ID` | unset | Replica identity — **user-global only**, never commit |

`output.title-length` and `agents.file` are functionally tool-level but
`bd config set` writes them to the database; they are read from `config.yaml`
when set there directly.

---

## 6. Agent policy profiles

Template profiles control how much text `bd setup` installs. **Policy profiles
control what an agent is authorized to do.**

| Profile | Scope | Git authority |
|---|---|---|
| `conservative` (default) | Standalone/unknown projects, one-off assistance | Use `bd` for tracking, then report changed files, validation, and proposed commands. No commit/push/`bd dolt push` without approval. |
| `minimal` | Hook-first integrations where `bd prime` carries the detail | Same authority; shorter installed text. |
| `team-maintainer` | Repos that explicitly delegate session close to agents | May close beads, run quality gates, commit, `bd dolt push`, `git push`. |

```bash
bd config set agent.profile team-maintainer
BD_AGENT_PROFILE=team-maintainer bd prime      # single process; env wins
```

An unrecognized value falls back to `conservative`. `bd prime` layers this on
top of hard per-branch constraints (stealth mode, no git remote, ephemeral
branch, `no-push`) — those always win, and an explicit "do not commit"
instruction outranks the profile. Beads never infers team-maintainer authority
merely because a remote exists.

---

## 7. Project-level (database) settings

| Namespace | Purpose |
|---|---|
| `jira.*`, `linear.*`, `github.*`, `gitlab.*`, `ado.*`, `notion.*` | Tracker integrations |
| `custom.*` | Your own keys |
| `<tracker>.last_sync` | Written automatically; enables incremental sync |
| `status.custom` | Custom statuses with behaviour categories |
| `types.custom` | Comma-separated custom issue types |
| `types.infra` | Types routed to the wisps table instead of the versioned issues table |
| `compact_tier1_days` / `compact_tier2_days` | Compaction age thresholds (30 / 90) |
| `issue_id_mode` | `hash` (default) or `counter` |
| `min_hash_length` / `max_hash_length` | Adaptive ID bounds (3 / 8) |
| `max_collision_prob` | Collision tolerance (0.25) |
| `doctor.suppress.<slug>` | Suppress a specific `bd doctor` **warning** |
| `mail.delegate` | Command `bd mail` delegates to (e.g. `gt mail`) |

The issue prefix (`issue_prefix`) is **not** settable here — use
`bd init --prefix`, `bd bootstrap`, or `bd rename-prefix`.

### Custom statuses and types

```bash
bd config set status.custom "in_review:active,qa_testing:wip,on_hold:frozen,archived:done"
bd config set types.custom  "agent,molecule,event"
bd statuses && bd types
```

Categories: `active` (in `bd ready` and default `bd list`), `wip` (list only),
`done` (terminal, hidden), `frozen` (on hold, hidden). No category = valid but
excluded from `bd ready`.

### Suppressing doctor warnings

```bash
bd config set doctor.suppress.pending-migrations true
bd config set doctor.suppress.git-hooks true
bd config unset doctor.suppress.git-hooks
```

Check names become slugs ("Git Hooks" → `git-hooks`). Only warnings are
suppressed; errors and passing checks always show.

---

## 8. Actor identity

Resolution order for `created_by` and the audit trail:

1. `--actor`
2. `BEADS_ACTOR`
3. `BD_ACTOR` (deprecated alias)
4. `git config user.name`
5. `$USER`
6. `"unknown"`

Usually nothing to configure — bead authorship matches commit authorship. For
an agent fleet, set a distinct identity per worker:

```bash
export BEADS_ACTOR="agent-worker-3"
```

---

## 9. Environment variables

Viper env prefix is `BD_`. YAML keys map by upper-casing and replacing `.` and
`-` with `_`: `dolt.auto-commit` → `BD_DOLT_AUTO_COMMIT`.

Commonly used:

| Variable | Purpose |
|---|---|
| `BEADS_DIR` | Force the active beads workspace directory |
| `BD_DB` / `BEADS_DB` | Database path (legacy `BEADS_DB` still honored) |
| `BD_JSON` | Force JSON output |
| `BEADS_ACTOR` / `BEADS_IDENTITY` | Identity |
| `BD_AGENT_PROFILE` | Agent policy profile |
| `BD_DOLT_AUTO_COMMIT` / `BD_DOLT_AUTO_PUSH*` | Commit/push behaviour |
| `BD_BACKUP_ENABLED` / `_INTERVAL` / `_GIT_REPO` | Backup |
| `BD_VALIDATION_ON_CREATE` / `_ON_CLOSE` / `_ON_SYNC` | Validation |
| `BD_FEDERATION_REMOTE` / `_SOVEREIGNTY` | Federation |
| `BD_NON_INTERACTIVE` | Disable prompts (also implied by no TTY or `CI=true`) |
| `BD_NO_PAGER` / `BD_PAGER` | Pager behaviour |
| `BD_DEBUG` | Debug logging |
| `BD_DEBUG_RPC` / `_SYNC` / `_ROUTING` / `_FRESHNESS` | Subsystem debug |
| `BD_JSON_ENVELOPE` | Opt into the v2 JSON envelope |
| `BD_SMART_GATE=0` | Make the remote-migrate gate block unconditionally |
| `BD_ALLOW_REMOTE_MIGRATE=1` | Declare this clone the designated migrator |
| `BD_EVENTS_JOURNAL*` | Events journal switches |
| `BEADS_NODE_ID` | Replica identity for lease reclaim |
| `BEADS_HOOK_TIMEOUT` | Git hook deadline in seconds (default 300) |
| `BEADS_FSCK_TIMEOUT` | Pre-push `dolt fsck` timeout (default 30s) |
| `BEADS_DOLT_SERVER_MODE` / `_HOST` / `_PORT` / `_USER` / `_SOCKET` / `_TLS` | Server mode |
| `BEADS_DOLT_PASSWORD` / `BEADS_CREDENTIALS_FILE` | Server auth |
| `BEADS_DOLT_SHARED_SERVER` / `BEADS_DOLT_DATA_DIR` / `BEADS_DOLT_BIN` | Dolt runtime |
| `DOLT_REMOTE_USER` / `DOLT_REMOTE_PASSWORD` | Remote clone/push/pull auth |
| `BD_OTEL_ENABLED` + `OTEL_*` | Telemetry (see `12-json-and-scripting.md`) |
| `CLAUDE_SESSION_ID` | Session id recorded by `bd close --session` |

Integration secrets follow tracker conventions: `LINEAR_API_KEY`,
`GITHUB_TOKEN`, `GITLAB_TOKEN`, `JIRA_API_TOKEN`, `AZURE_DEVOPS_PAT`,
`ANTHROPIC_API_KEY`, `NOTION_TOKEN`.

`BEADS_DOLT_BIN` pins the exact external `dolt` binary for managed
proxied-server mode, overriding PATH lookup; an explicit path that fails
validation is an error, not a silent fallback.

---

## 10. Server credentials file

Instead of per-project env vars, store passwords keyed by `host:port`:

```ini
# ~/.config/beads/credentials      (chmod 600)
[127.0.0.1:3307]
password=localDevPassword

[beads.company.com:3307]
password=teamServerPassword
```

Resolution: `BEADS_DOLT_PASSWORD` → credentials-file lookup → empty. The
lookup uses the **resolved runtime port** (port file, env var, then config),
not necessarily the port in `metadata.json` — which matters behind IAP tunnels.
Default location `~/.config/beads/credentials` (`%APPDATA%\beads\credentials`
on Windows); override with `BEADS_CREDENTIALS_FILE`. A warning is printed if
the file is group/world readable.

---

## 11. Where secrets live

- Tokens and API keys are **never** stored in the Dolt database.
- `bd config set` routes secret keys to the local `config.yaml`.
- Writing a secret to a **git-tracked** `config.yaml` is refused without
  `--force-git-tracked`.
- Environment variables are the safer default.
- `bd init` writes `.beads/.gitignore` covering the database directories,
  runtime files, push state, and the federation credential key.

---

## 12. Example project `config.yaml`

```yaml
# .beads/config.yaml — committed, team-shared
json: false

dolt:
  auto-commit: on
  auto-push: false

create:
  require-description: true

validation:
  on-create: warn
  on-close: none

backup:
  enabled: true
  interval: 15m

export:
  auto: false          # turn on only if a viewer or hydration consumes it
  path: issues.jsonl
  interval: 60s
  git-add: false

hierarchy:
  max-depth: 3

list:
  limit: 50

# Monorepo: label beads by the directory they concern
directory:
  labels:
    packages/web: web
    packages/api: api

# Cross-project capability dependencies
external_projects:
  backend: ../backend
```

Machine-specific overrides that must not be committed go in
`.beads/config.local.yaml`, and per-machine identity (`node_id`) goes in
`~/.config/bd/config.yaml`.

---

## 13. Tracker configuration quick reference

```bash
# Jira
bd config set jira.url "https://company.atlassian.net"
bd config set jira.project "PROJ"            # or jira.projects "P1,P2"
export JIRA_API_TOKEN=...                    # preferred over config
bd config set jira.status_map.open "To Do"
bd config set jira.type_map.feature "Story"
bd config set jira.custom_fields.customfield_10042 '{"value":"AI Platform"}'
bd config set jira.push_prefix "hippo"       # only push hippo-* beads

# Linear
export LINEAR_API_KEY=lin_api_...
bd config set linear.team_id "<uuid>"        # or linear.team_ids "u1,u2"
bd config set linear.state_map.in_review in_progress
bd config set linear.label_type_map.bug bug
bd config set linear.relation_map.blocks blocks
bd linear teams                              # discover team UUIDs

# GitHub
bd config set github.org myorg && bd config set github.repo myrepo
export GITHUB_TOKEN=...
bd config set github.label_map.feature enhancement

# Azure DevOps
bd config set ado.org myorg && bd config set ado.project MyProject
export AZURE_DEVOPS_PAT=...

# Notion
bd config set notion.token <token>           # or NOTION_TOKEN
bd notion init --parent <page-id>            # or: bd notion connect --url <url>
bd notion status --json
```

Linear staleness detection writes `.beads/last_pull` (local-only) after each
pull; `bd linear sync --pull-if-stale --threshold 20m` pulls only when data is
old, with a 5-minute debounce to prevent agent loops. Core `bd` commands never
contact Linear — run the stale-pull from a session-start hook if you need
freshness.
