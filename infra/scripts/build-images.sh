#!/usr/bin/env bash
# Build the Livepeer Open Clearinghouse gateway image.
#
# Usage:
#   ./infra/scripts/build-images.sh
#   PUSH=1 TAG=v2.0.1 ./infra/scripts/build-images.sh
#
# Env:
#   REGISTRY    default: tztcloud
#   IMAGE       default: livepeer-open-clearinghouse-gateway
#   IMAGE_NAME  optional full repository override (takes precedence)
#   TAG         default: dev
#   VERSION     default: derived from the Git tag/commit
#   PUSH        1 publishes after building; default: 0
#
# The GitHub release workflow remains authoritative for multi-architecture
# release images, provenance, and SBOMs. This script is the reproducible local
# and operator build boundary. A push refuses dirty source and refuses to move
# an existing SemVer Git tag to content built from another commit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION_ENV_FILE="${ROOT}/infra/build/image-versions.env"
if [[ -f "$VERSION_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$VERSION_ENV_FILE"
fi

REGISTRY="${REGISTRY:-tztcloud}"
IMAGE="${IMAGE:-livepeer-open-clearinghouse-gateway}"
IMAGE_NAME="${IMAGE_NAME:-${REGISTRY}/${IMAGE}}"
TAG="${TAG:-${IMAGE_TAG_DEFAULT:-dev}}"
PUSH="${PUSH:-0}"
REVISION="$(git rev-parse HEAD)"
DEFAULT_VERSION="$(VERSION_PREFIX="$TAG" FALLBACK_VERSION="$TAG" ./infra/build/git-version.sh)"
VERSION="${VERSION:-$DEFAULT_VERSION}"

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

case "$PUSH" in
  0|1) ;;
  *) fail "PUSH must be 0 or 1, got ${PUSH}" ;;
esac

[[ -n "$IMAGE_NAME" ]] || fail "IMAGE_NAME must not be empty"
[[ -n "$TAG" ]] || fail "TAG must not be empty"

if [[ "$PUSH" == "1" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    printf '\033[1;31m[fail]\033[0m refusing to push: working tree has uncommitted changes\n' >&2
    printf '       version would be %s, which no commit can reproduce.\n' "$VERSION" >&2
    git status --short >&2
    exit 1
  fi

  # Never silently republish a released version from different source. The
  # tag may be local or fetched; either way its commit is the release identity.
  if [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-].*)?$ ]] && git rev-parse -q --verify "refs/tags/${TAG}^{commit}" >/dev/null; then
    tagged_revision="$(git rev-list -n 1 "$TAG")"
    if [[ "$tagged_revision" != "$REVISION" ]]; then
      fail "refusing to move release ${TAG}: it points to ${tagged_revision}, current source is ${REVISION}"
    fi
  fi
fi

full_tag="${IMAGE_NAME}:${TAG}"
build_args=(
  build
  -t "$full_tag"
  -f Dockerfile
  "--build-arg=PYTHON_VERSION=${PYTHON_VERSION:-3.13}"
  "--build-arg=UV_VERSION=${UV_VERSION:-0.5}"
  "--build-arg=VERSION=${VERSION}"
  "--build-arg=REVISION=${REVISION}"
  .
)

log "image=${full_tag} version=${VERSION} revision=${REVISION} push=${PUSH}"
docker "${build_args[@]}" || fail "build failed for ${full_tag}"
ok "built ${full_tag}"

if [[ "$PUSH" == "1" ]]; then
  log "pushing ${full_tag}"
  docker push "$full_tag" || fail "push failed for ${full_tag}"
  digest="$(docker inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$full_tag" 2>/dev/null || true)"
  ok "pushed ${full_tag}"
  printf '\nPin this digest in deployment; do not deploy a mutable tag:\n  %s\n' "${digest:-$full_tag (digest unavailable)}"
fi
