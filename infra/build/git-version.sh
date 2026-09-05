#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION_PREFIX="${VERSION_PREFIX:-}"
FALLBACK_VERSION="${FALLBACK_VERSION:-dev}"

git_short_sha="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
git_exact_tag="$(git describe --tags --exact-match 2>/dev/null || true)"
git_dirty_suffix=""
if [[ -n "$git_short_sha" ]] && [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  git_dirty_suffix="-dirty"
fi

if [[ -n "$git_exact_tag" ]]; then
  printf '%s%s\n' "$git_exact_tag" "$git_dirty_suffix"
elif [[ -n "$git_short_sha" ]]; then
  if [[ -n "$VERSION_PREFIX" && "$VERSION_PREFIX" != "dev" && "$VERSION_PREFIX" != "$git_short_sha" ]]; then
    printf '%s-%s%s\n' "$VERSION_PREFIX" "$git_short_sha" "$git_dirty_suffix"
  else
    printf '%s%s\n' "$git_short_sha" "$git_dirty_suffix"
  fi
else
  printf '%s\n' "$FALLBACK_VERSION"
fi
