# 10 — Harness integration

Wiring beads into agent harnesses. The three that matter here are **Claude
Code**, **Codex**, and the **MCP server**; the `bd setup` recipe system covers
the rest.

---

## 1. CLI + hooks beats MCP

Where a shell exists, use the CLI:

| | CLI + hooks | MCP server |
|---|---|---|
| Context overhead | ~1–2k tokens (`bd prime`) | 10–50k tokens of tool schemas |
| Latency | direct process calls | MCP protocol round-trips |
| Availability | anything with a shell | MCP-capable clients |
| Setup | one hook | client config |

Compute cost scales with tokens, latency grows with context, and models attend
better to smaller contexts. Reach for MCP only when there is no shell (Claude
Desktop, Amp without shell).

---

## 2. `bd setup` — the recipe system

```bash
bd setup --list                # every recipe your binary supports
bd setup <recipe>              # install
bd setup <recipe> --check      # verify (reports missing | stale | current)
bd setup <recipe> --remove     # uninstall, leaving the rest of the file intact
bd setup --print               # print the template
bd setup -o path/to/file.md    # one-off write, no recipe saved
bd setup --add myeditor .myeditor/rules.md   # save a custom recipe
```

| Recipe | Files written |
|---|---|
| `claude` | `.claude/settings.json` (or `~/.claude/settings.json` with `--global`) + a managed `CLAUDE.md` section |
| `codex` | `.agents/skills/beads/` + a managed `AGENTS.md` section + `.codex/config.toml` + `.codex/hooks.json` |
| `cursor` | `.cursor/rules/beads.mdc` (+ `.cursor/hooks.json`) |
| `gemini` | `~/.gemini/settings.json` (or `.gemini/settings.json` with `--project`) + a `GEMINI.md` section |
| `copilot` | `.copilot-plugin/plugin.json` + `.github/copilot-instructions.md` |
| `factory` / `mux` / `opencode` | Managed `AGENTS.md` section (mux also `.mux/` layers) |
| `aider` | `.aider.conf.yml` + `.aider/BEADS.md` + `.aider/README.md` |
| `junie` | `.junie/guidelines.md` + `.junie/mcp/mcp.json` |
| `windsurf` / `cody` / `kilocode` / `kiro` | A single rules file under the tool's directory |

Recipe types: `file` (write one file), `hooks` (patch JSON settings),
`section` (inject a marked section), `multifile`. Custom recipes added with
`--add` are always `file` type and are stored in `.beads/recipes.toml` (adding
one requires an active workspace):

```toml
[recipes.myeditor]
name = "myeditor"
path = ".myeditor/rules.md"
type = "file"
```

### Managed sections

`factory`, `mux`, and `opencode` append a section to `AGENTS.md` wrapped in
`BEGIN/END BEADS INTEGRATION` HTML comments. The begin marker carries version,
profile, and hash metadata — e.g.
`<!-- BEGIN BEADS INTEGRATION v:1 profile:full hash:19cc25d9 -->` — so
`--check` can report `missing`, `stale`, or `current`. Re-running setup updates
in place; `--remove` deletes only the managed section.

`bd setup codex` uses its own marker pair (`BEGIN/END BEADS CODEX SETUP`), so
running it alongside `factory`/`mux` leaves two managed sections side by side,
each with independent `--check` and `--remove`.

One `AGENTS.md` serves Factory Droid, Mux, OpenCode, Cursor, Zed, Jules, and
other AGENTS.md-aware tools — a good starting point for mixed-tool teams.

### Template profiles

| Profile | Used by | Content |
|---|---|---|
| `full` | Factory, Mux, OpenCode | Complete command reference, types, priorities, workflow |
| `minimal` | Claude Code, Copilot CLI, Gemini CLI | A pointer to `bd prime` plus a quick reference (~60% smaller) |

Hook-enabled agents get `minimal` because `bd prime` injects full context at
session start; AGENTS-first agents get `full` because the file *is* the
integration surface. If a file already carries a `full` section and a `minimal`
tool installs to the same path, `full` is preserved.

Codex is skill-based instead: `.agents/skills/beads/SKILL.md` plus managed
`AGENTS.md` guidance telling Codex when to use it.

**Commit the instruction files** (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) so
every teammate and tool gets the same rules.

---

## 3. Claude Code

```bash
bd setup claude              # project: .claude/settings.json + CLAUDE.md section
bd setup claude --global     # ~/.claude/settings.json
bd setup claude --check
bd setup claude --remove
bd setup claude --stealth    # bd prime --stealth --hook-json: flush only, no git ops
```

This installs a **SessionStart hook** running `bd prime --hook-json`.
SessionStart fires on start, resume, clear, **and after context compaction**,
so no separate `PreCompact` hook is needed. `--hook-json` wraps the markdown in
the JSON envelope Claude Code expects.

Manual equivalent:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "bd prime --hook-json" }
        ]
      }
    ]
  }
}
```

Restart Claude Code after installing, then `bd setup claude --check`.

The `CLAUDE.md` section is managed with hash/version markers for safe updates,
and is skipped if `CLAUDE.md` is a symlink.

### The upstream plugin, and why this skill assumes it is absent

Beads ships an optional Claude Code plugin:

```
/plugin marketplace add gastownhall/beads
/plugin install beads
```

It adds `/beads:ready`, `/beads:create`, `/beads:show`, `/beads:update`,
`/beads:close`, `/beads:init`, `/beads:workflow`, `/beads:stats`,
`/beads:version`, a `@task-agent` subagent, its own bundled skill, and
**SessionStart + PreCompact hooks that both run `bd prime`**.

If the plugin is enabled, `bd setup claude` deliberately skips writing hooks so
`bd prime` does not fire twice per session.

This skill is a standalone replacement. Pick one hook owner:

- **Recommended:** do not install the plugin. Use `bd setup claude` for the
  SessionStart hook and this skill for the workflow.
- If you want the plugin's slash commands, skip `bd setup claude` and let the
  plugin own the hooks; this skill still works, but expect overlapping guidance
  from the plugin's own bundled skill.

The plugin does not bundle an MCP server — it drives the `bd` CLI. Configure
`beads-mcp` separately only if you want MCP tools too.

### Installing this skill

```bash
# Project scope
mkdir -p .claude/skills
cp -r /path/to/beads-skill/skills/beads .claude/skills/

# User scope (all projects)
mkdir -p ~/.claude/skills
cp -r /path/to/beads-skill/skills/beads ~/.claude/skills/
```

The `references/` files must travel with `SKILL.md` — they are what the routing
table points at.

---

## 4. Codex

```bash
bd setup codex
bd setup codex --check
bd setup codex --global      # writes under $CODEX_HOME, else ~/.codex
```

Project setup writes:

- `.agents/skills/beads/` — the Beads skill
- `AGENTS.md` — a managed Beads section
- `.codex/config.toml` — `[features] hooks = true`
- `.codex/hooks.json` — the hook configuration

`bd init` runs this automatically unless `--skip-agents` or `--stealth`.

Codex 0.129.0+ supports `/hooks`, compact lifecycle hooks, and hook-provided
developer context. The lifecycle beads uses:

| Hook | Matcher | Behaviour |
|---|---|---|
| `SessionStart` | `startup\|resume\|clear` | Injects full `bd prime` output |
| `PreCompact` | `manual\|auto` | Checks `bd prime --memories-only`, warns if beads context is unavailable |
| `PostCompact` | `manual\|auto` | Records that the session needs a beads refresh |
| `UserPromptSubmit` | — | Injects full `bd prime` once after compaction, then clears the marker |

`PreCompact` alone cannot inject context because Codex ignores plain stdout
from compact hooks; the post-compact marker plus first-prompt refresh is the
reliable recovery path. Markers live in a user cache/temp directory keyed by
Codex `session_id` and workspace path — never in tracked files or the database.

Manual `.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume|clear",
        "hooks": [{ "type": "command", "command": "bd codex-hook SessionStart",
                    "statusMessage": "Loading Beads context" }] }
    ],
    "PreCompact": [
      { "matcher": "manual|auto",
        "hooks": [{ "type": "command", "command": "bd codex-hook PreCompact",
                    "statusMessage": "Checking Beads context" }] }
    ],
    "PostCompact": [
      { "matcher": "manual|auto",
        "hooks": [{ "type": "command", "command": "bd codex-hook PostCompact",
                    "statusMessage": "Scheduling Beads context refresh" }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "bd codex-hook UserPromptSubmit",
                    "statusMessage": "Refreshing Beads context" }] }
    ]
  }
}
```

plus:

```toml
# .codex/config.toml
[features]
hooks = true
```

Use `/hooks` inside Codex to inspect or toggle the installed handlers.

To use *this* skill under Codex, place it where Codex reads skills
(`.agents/skills/beads/`) or point the managed `AGENTS.md` section at it.

---

## 5. MCP server

For MCP-only environments.

```bash
uv tool install beads-mcp     # recommended
pip install beads-mcp
```

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{ "mcpServers": { "beads": { "command": "beads-mcp" } } }
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json` in the project, or the
user-level `mcp.json` (`~/Library/Application Support/Code/User/mcp.json`,
`~/.config/Code/User/mcp.json`, `%APPDATA%\Code\User\mcp.json`). Requires
VS Code 1.96+.

```json
{ "servers": { "beads": { "command": "beads-mcp", "args": [] } } }
```

**Sourcegraph Amp**: `{ "beads": { "command": "beads-mcp", "args": [] } }`.

### Tools exposed

`ready`, `list`, `show`, `create`, `claim`, `update`, `close`, `reopen`, `dep`,
`comment`, `comments`, `note`, `blocked`, `stats`, `context`, `admin`,
`discover_tools`, `get_tool_info`.

**There is no MCP sync tool** — syncing stays on the CLI (`bd dolt push` /
`bd dolt pull`).

### Behaviour worth knowing

- The server is a **stateless adapter**: it translates MCP calls into `bd` CLI
  invocations and routes each call to the correct `.beads` workspace based on
  the working directory. It never caches or stores issue data.
- Run **one** instance across all projects, not one per project.
- `bd prime` auto-detects MCP and switches to a ~50-token reminder instead of
  the full command reference. Force either mode with `--mcp` / `--full`.
- Claude Code prompts before each MCP tool call unless you opt out:
  `{"enabledMcpjsonServers": ["beads"]}` (auto-approve beads) or
  `{"enableAllProjectMcpServers": true}`. There is no per-tool granularity —
  auto-approval is all-or-nothing per server.
- If `beads-mcp` fails to start, the usual cause is `uv` missing from `PATH`.

---

## 6. Other harnesses in one line each

| Tool | Setup | Notes |
|---|---|---|
| **Cursor** | `bd setup cursor` | `.cursor/rules/beads.mdc`, re-included every turn |
| **Windsurf** | `bd setup windsurf` | `.windsurf/rules/beads.md` |
| **Gemini CLI** | `bd setup gemini [--project]` | SessionStart hook with `--hook-json` (Gemini requires JSON stdout) + `GEMINI.md` section |
| **Copilot CLI** | `bd setup copilot` | `.copilot-plugin/plugin.json` registers `bd prime` hooks; project-scoped only |
| **Copilot in VS Code** | MCP | `.vscode/mcp.json` |
| **Aider** | `bd setup aider` | Human-in-the-loop: the AI *suggests* `bd` commands, you run them with `/run` |
| **Factory Droid / Mux / OpenCode** | `bd setup factory\|mux\|opencode` | Managed `AGENTS.md` section, `full` profile |
| **Junie** | `bd setup junie` | `.junie/guidelines.md` + MCP config |
| **Cody / Kilo Code / Kiro** | `bd setup cody\|kilocode\|kiro` | Single rules file |

In worktree, shared, or `BEADS_DIR` setups, confirm the resolved workspace with
`bd where` — these integrations do not require a local `./.beads`. Restart the
tool after setup if it is already running.

---

## 7. `bd prime` — the context contract

```bash
bd prime                  # full CLI context (~1–2k tokens)
bd prime --hook-json      # wrapped for SessionStart hooks
bd prime --memories-only  # just the memories, for compact hooks
bd prime --stealth        # no git operations in the close protocol
bd prime --full           # force full output even under MCP
bd prime --mcp            # force minimal MCP-mode output
bd prime --export         # dump the default content for customization
```

What it prints: a truncation warning, your persistent memories, the session
close protocol, core rules, the essential command set, quality/lifecycle
commands, and common workflows — adapted to the detected environment (git
remote present or not, git authority, MCP or CLI).

Two customization hooks:

- `.beads/PRIME.md` in the local clone or resolved workspace **replaces** the
  output entirely. Start from `bd prime --export`.
- `bd config set no-git-ops true` removes git commands from the close protocol
  when you want to control commits manually.
- `prime.max-memories` / `prime.max-memory-chars` bound the memory section.

If your host stores hook output in a file and shows only a preview, read the
full file — that is what the leading truncation warning is for.

---

## 8. Manual onboarding for unsupported tools

```bash
bd onboard        # prints the ~10-line minimal snippet
```

Paste it into whatever file the tool reads. The minimal snippet points at
`bd prime` rather than duplicating the command reference, which keeps the
instruction file lean and always current. For tools that cannot run hooks, use
`bd init --agents-profile=full` to embed the full reference instead.

---

## 9. Verifying an integration

```bash
bd version
bd doctor                  # includes integration status
bd hooks list
bd setup claude --check    # or codex / cursor / gemini / ...
bd prime                   # must succeed standalone
bd where                   # correct workspace?
```

- *Hooks not firing?* Restart the tool, re-run `--check`, read `bd doctor`.
- *Context missing?* Make sure `bd prime` works standalone; if it fails, fix
  the underlying beads problem first.
- *`bd prime` running twice?* Both the plugin and `bd setup claude` installed
  hooks — remove one.
