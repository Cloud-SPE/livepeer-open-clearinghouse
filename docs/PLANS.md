# Planning and work tracking

Livepeer Open Clearinghouse uses [Beads](https://github.com/gastownhall/beads)
(`bd`) as the only source of truth for open, blocked, deferred, and in-progress
work. The project-local operating manual is
[`../.agents/skills/beads/SKILL.md`](../.agents/skills/beads/SKILL.md).

## Starting and resuming work

```bash
bd prime
bd ready
bd list --status in_progress
bd blocked
```

Claim one ready bead before editing code:

```bash
bd show <id> --json
bd update <id> --claim
```

Every bead needs a specific title, a description that supplies the missing
context, a real type and numeric priority, and acceptance or success criteria.
Search before creating one. Record newly discovered work immediately with a
`discovered-from` edge instead of silently widening scope.

## Planning multi-step work

Use an epic for a substantial feature, migration, or cross-domain project.
Create concrete children, then encode actual prerequisites with blocking
edges. Hierarchical numbering does not imply order.

```bash
EPIC=$(bd q "Payments v2" --type epic --priority 1)
bd create "Agree wire contract" --type decision --priority 0 --parent "$EPIC" \
  --description "..." --acceptance "..."
bd create "Implement provider changes" --type feature --priority 1 \
  --parent "$EPIC" --description "..." --acceptance "..."
bd dep add "$EPIC.2" "$EPIC.1"  # implementation NEEDS the decision
bd blocked
bd ready --parent "$EPIC"
```

Dependency direction always means requirement: `bd dep add A B` means “A
needs B.” Run `bd blocked`, `bd ready`, and `bd graph check` after wiring a
large graph.

## What belongs in Markdown

Repository documents remain the system of record for durable knowledge:

- Product specs define user-visible behavior.
- Design docs explain load-bearing decisions and rejected alternatives.
- Pillar docs define architecture, reliability, security, and product rules.
- `docs/exec-plans/completed/` retains historical implementation narratives.

Markdown is not a work tracker. Do not create active exec-plan checklists,
scratch plan files, `TODO.md`, or a second tech-debt list. When a bead produces
a durable decision, update the appropriate design or pillar doc and link that
artifact from the bead.

The active exec-plan directory is retained only for repository history; do not
add new trackers there. `docs/exec-plans/tech-debt-tracker.md` is a legacy
input being migrated under Beads epic `loc-5vm`; no new work should be added
to it.

## Finishing and handing off

Record progress with `bd note <id> "..."`. Close only work that is actually
complete, always with a reason, then inspect what became ready:

```bash
bd close <id> --reason "Implemented and verified ..."
bd ready
bd lint
bd graph check
```

`bd prime` prints the repository's current Git and Dolt authority. Do not run
`bd dolt push`, commit, or push merely because the tracker exists; follow that
authority and the user's instructions.
