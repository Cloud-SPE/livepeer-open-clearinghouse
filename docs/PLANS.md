# PLANS.md

How planning works in Livepeer Open Clearinghouse. Plans are first-class repository artifacts:
they're versioned, reviewable, and discoverable by future agent runs.

## When to write a plan vs. when not

**Don't write a plan when:**
- The change fits in one or two files and is mechanical (rename, typo,
  one-line fix, dependency bump).
- The change is purely documentation.
- The change is reverting a recent change.

**Write a *lightweight plan* when:**
- Touching 3+ files in one domain.
- Adding a new endpoint or migration.
- Refactoring an existing pattern in place.

A lightweight plan is a short scratch file (50–150 lines) that lives in the
working directory of the change (or in `docs/exec-plans/active/` if
non-trivial). It captures: intent, files to touch, the steps in order, and
what "done" looks like. It can be deleted after merge.

**Write an *exec-plan* when:**
- Adding a new domain.
- Changing the layered architecture or the provider interface.
- Touching the billing math, the ticket-mint flow, or anything that affects
  user-visible accounting.
- Coordinating work across more than one domain.
- Any change where reviewers (human or agent) need a written record of *why*.

Exec-plans live in `docs/exec-plans/active/` while in flight, then move to
`docs/exec-plans/completed/` when shipped.

## Exec-plan template

A new exec-plan starts as a single markdown file:
`docs/exec-plans/active/NNN-short-slug.md` (sequentially numbered).

```markdown
# NNN. <short title>

**Status:** draft | in-progress | blocked | complete
**Owner:** <agent or human handle>
**Opened:** YYYY-MM-DD
**Closed:** YYYY-MM-DD (when complete)

## Intent

One paragraph: what changes after this lands, and why now.

## Scope

- In: bullet list of what this plan covers
- Out: bullet list of what this plan explicitly does not cover

## Approach

The plan, broken into phases or steps. Each step is concrete enough that
a fresh reader could execute it. Cross-link to specific files where helpful.

## Decisions

A running log of design decisions made while executing this plan, with
*why*, not just *what*. Append-only.

## Open questions

Things that need an answer before the plan can complete. Resolved questions
move into "Decisions."

## Validation

How "done" is verified. Tests added, integration tests run, manual flows
checked, observability signals confirmed.

## Follow-ups

Items deliberately punted, with where they go (tech-debt-tracker.md, a new
exec-plan, etc.).
```

## Plan hygiene

- **One plan per artifact.** Do not bundle unrelated changes.
- **Update the plan in place.** When new information arrives or an approach
  changes, edit the plan file. Add to "Decisions"; don't rewrite history.
- **Close plans when they're done.** Move the file to
  `docs/exec-plans/completed/` and update its `Status` and `Closed` fields.
  A completed plan is documentation: it lets the next agent run understand
  what shipped and why.
- **If a plan is abandoned, mark it abandoned.** Move to `completed/` with
  `Status: abandoned` and a note on the "Decisions" log explaining why.

## Tech-debt tracker

`docs/exec-plans/tech-debt-tracker.md` is the registry of known deferrals
and identified debt. Items in there are not in-flight; they're
acknowledgements that something is suboptimal and a marker for future work.

Add to it when a plan deliberately defers something. Remove from it when
the debt is paid down (and reference the plan that paid it down).

## Don't write a plan after the fact

If you find yourself writing an exec-plan to document a change that has
already shipped, you're writing the wrong artifact. Write a design doc
(`docs/design-docs/`) or update the relevant pillar doc instead.
