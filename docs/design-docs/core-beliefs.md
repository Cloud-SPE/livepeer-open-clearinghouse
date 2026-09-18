# 000. Core beliefs

**Status:** accepted
**Opened:** 2026-05-22

The agent-first operating principles for working in this repository. These
beliefs precede any specific design decision. They are how we decide.

## 1. Knowledge in the repo or it doesn't exist

If a fact, decision, convention, or rationale isn't written in `docs/` or
in code, an agent cannot see it. Slack messages, Google Docs, hallway
chats, and "the way Bob always did it" are invisible to Livepeer Open Clearinghouse's
primary contributor.

So: write things down here. When a discussion produces a decision, the
decision lives in a design doc or a pillar doc. When a convention emerges,
it's documented before it's enforced.

## 2. Parse at boundaries, don't validate

Every byte that enters Livepeer Open Clearinghouse from the outside world — HTTP body,
headers, environment variable, daemon response — gets parsed into a
typed structure at the boundary. Past the boundary, code trusts types.

The wrong shape is:

```python
def handle(req: dict):
    if "capability" not in req: raise ValueError(...)
    if not isinstance(req["capability"], str): raise ValueError(...)
    ...
```

The right shape is:

```python
class MintRequest(BaseModel):
    capability: str
    offering: str
    work_units: int = Field(gt=0)

def handle(req: MintRequest):
    # all fields are typed and validated by construction
```

## 3. Strict layering, mechanically enforced

`types → config → repo → service → runtime → ui`. Each domain. No
exceptions. Cross-cutting concerns enter only through `providers/`.

We choose this not because it's "clean architecture" but because:

- It's a rule an agent can hold in its head and apply uniformly.
- It localizes blast radius: a `service.py` change cannot accidentally
  break HTTP routing or break the DB schema.
- It's enforceable with a linter, so drift is detectable.

## 4. Boring tech, latest versions

We prefer technologies that are:

- Composable (pieces can be replaced without rewriting the system).
- Stable (API changes are predictable, training data is plentiful).
- Well-represented in training corpora (the agent has seen it).

That means FastAPI, SQLAlchemy 2.0, Postgres, Lit, vanilla CSS. Not a
clever new framework that solves a problem we don't have.

"Latest versions" means we keep dependencies current. The cost of an
agent struggling with stale APIs is higher than the cost of a periodic
dependency bump.

## 5. The work graph is first-class

Anything more than a small change gets a Beads issue before code. Anything
cross-domain or dependency-heavy gets an epic whose edges state what each
child needs. See `docs/PLANS.md`.

The graph preserves execution state; product, design, and pillar docs preserve
the durable reason the diff looks the way it does six months later.

## 6. Fail closed

In any path that touches money, identity, or external trust: when in
doubt, fail. Returning an error is a signal the system can recover from.
Serving work without recording payment is a state the system cannot
recover from cleanly.

## 7. Three lines is better than a premature abstraction

If you see three similar lines of code, don't immediately extract a
helper. Three similar lines is a pattern. Five similar lines is also
probably still a pattern. The cost of a bad abstraction is paid every
time someone has to read it. The cost of duplication is paid once,
when you actually need to change it.

## 8. Comments explain *why*, not *what*

A comment that restates the next line is noise. A comment that explains
a non-obvious constraint, a workaround for a specific bug, or a subtle
invariant is signal.

Default to no comment. Add one when the absence of one would surprise the
next reader.

## 9. Don't build for hypothetical futures

We are building MVP. We are not building "the system that someday might
support multi-tenancy / OIDC / per-key billing / horizontal scale." We
are building the system that does the thing today, with the doors not
locked against doing those things later.

When a hypothetical-future argument starts to drive a design choice, create a
low-priority Beads issue with its trigger and move on.

## 10. Garbage-collect entropy continuously

Drift accumulates. Stale docs, dead code, inconsistent patterns, half-
finished refactors — they compound. The cheapest moment to clean
something up is right after you notice it. The most expensive moment is
"in a few weeks when we have time."

Doc-gardening passes, lint cleanups, and dead-code removal are normal
work, not special projects.
