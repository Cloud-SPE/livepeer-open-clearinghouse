# 001. Livepeer Open Clearinghouse Rename

**Status:** complete
**Owner:** Codex
**Opened:** 2026-05-23
**Closed:** 2026-05-23

## Intent

Standardize the product and codebase on the Livepeer Open Clearinghouse
name across docs, Python package paths, examples, frontend branding, and
local dev configuration so the repository uses one consistent identity.

## Scope

- In: user-facing product name, code package/module names, examples, compose
  and image names, docs, tests, and local configuration strings
- Out: API route redesigns, behavioral changes, and generated docs refreshes

## Approach

1. Rename primary filesystem/package identifiers to the new canonical slug.
2. Update in-file imports, branding text, config defaults, SDK package names,
   and operational identifiers.
3. Run targeted search passes to remove old names.
4. Run tests/lint where practical and note any remaining gaps.

## Done Looks Like

- No remaining repo references to legacy product naming
- App/package entrypoints resolve under the new name
- Tests that cover imports and basic flows still pass
