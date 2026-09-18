#!/usr/bin/env bash
# Restore a consistent v1 production dump into an isolated Postgres and migrate it to v2.
#
# Required env:
#   SOURCE_DATABASE_URL  Read-only PostgreSQL connection URL for the quiesced v1 database.
#   ARTIFACT_DIR         New or existing directory for dump, audit, and migration evidence.
#
# The source is never migrated. The only writes are to ARTIFACT_DIR and an ephemeral
# localhost-only Postgres container created by this script.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

SOURCE_DATABASE_URL="${SOURCE_DATABASE_URL:-}"
ARTIFACT_DIR="${ARTIFACT_DIR:-}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16-alpine}"

fail() { printf '[fail] %s\n' "$*" >&2; exit 1; }
log()  { printf '[migration] %s\n' "$*" >&2; }

[[ -n "$SOURCE_DATABASE_URL" ]] || fail "SOURCE_DATABASE_URL is required"
[[ -n "$ARTIFACT_DIR" ]] || fail "ARTIFACT_DIR is required"
command -v pg_dump >/dev/null || fail "pg_dump is required"
command -v pg_restore >/dev/null || fail "pg_restore is required"
command -v docker >/dev/null || fail "docker is required"

mkdir -p "$ARTIFACT_DIR"
ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd)"
container_name="loc-v2-migration-rehearsal-$$"
staging_password="$(openssl rand -hex 24)"
connection_dir="$(mktemp -d)"
source_service_file="$connection_dir/pg_service.conf"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  find "$connection_dir" -type f -delete >/dev/null 2>&1 || true
  rmdir "$connection_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT

SOURCE_DATABASE_URL="$SOURCE_DATABASE_URL" uv run python - "$source_service_file" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

url = urlsplit(os.environ["SOURCE_DATABASE_URL"])
if url.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("SOURCE_DATABASE_URL must use postgres:// or postgresql://")
if not all((url.hostname, url.username, url.path.removeprefix("/"))):
    raise SystemExit("SOURCE_DATABASE_URL must include host, user, and database")

values = {
    "host": url.hostname,
    "port": str(url.port or 5432),
    "user": unquote(url.username),
    "dbname": unquote(url.path.removeprefix("/")),
}
if url.password is not None:
    values["password"] = unquote(url.password)
for key, items in parse_qs(url.query, keep_blank_values=True).items():
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or len(items) != 1:
        raise SystemExit(f"unsupported PostgreSQL URL option: {key}")
    values[key] = items[0]

def escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SystemExit("PostgreSQL URL fields must not contain newlines")
    return value.replace("\\", "\\\\")

path = Path(sys.argv[1])
path.write_text(
    "[loc_source]\n" + "".join(f"{key}={escape(value)}\n" for key, value in values.items()),
    encoding="utf-8",
)
path.chmod(0o600)
PY

log "taking a consistent, read-only v1 dump"
dump_start_ns="$(date +%s%N)"
PGSERVICE=loc_source PGSERVICEFILE="$source_service_file" pg_dump \
  --format=custom \
  --no-owner \
  --no-acl \
  --serializable-deferrable \
  --file="$ARTIFACT_DIR/v1.dump"
dump_end_ns="$(date +%s%N)"
sha256sum "$ARTIFACT_DIR/v1.dump" > "$ARTIFACT_DIR/v1.dump.sha256"
pg_restore --list "$ARTIFACT_DIR/v1.dump" > "$ARTIFACT_DIR/v1.dump.list"

log "starting isolated Postgres ${POSTGRES_IMAGE}"
docker run -d --rm \
  --name "$container_name" \
  -e POSTGRES_DB=loc_migration \
  -e POSTGRES_USER=loc_migration \
  -e "POSTGRES_PASSWORD=$staging_password" \
  -p 127.0.0.1::5432 \
  "$POSTGRES_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$container_name" pg_isready -U loc_migration -d loc_migration >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container_name" pg_isready -U loc_migration -d loc_migration >/dev/null \
  || fail "isolated Postgres did not become ready"

staging_port="$(docker port "$container_name" 5432/tcp | sed 's/.*://')"
staging_url="postgresql+asyncpg://loc_migration:${staging_password}@127.0.0.1:${staging_port}/loc_migration"

log "restoring dump"
restore_start_ns="$(date +%s%N)"
PGHOST=127.0.0.1 PGPORT="$staging_port" PGUSER=loc_migration \
PGPASSWORD="$staging_password" PGDATABASE=loc_migration pg_restore \
  --dbname=loc_migration \
  --exit-on-error \
  --no-owner \
  --no-acl \
  "$ARTIFACT_DIR/v1.dump"
restore_end_ns="$(date +%s%N)"

log "auditing restored v1 data"
DATABASE_URL="$staging_url" uv run python scripts/postgres_migration_audit.py snapshot \
  --phase pre \
  --output "$ARTIFACT_DIR/pre.json"

head_revision="$(uv run alembic heads | awk '{print $1}')"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
migration_start_ns="$(date +%s%N)"
log "migrating isolated restore to Alembic ${head_revision}"
DATABASE_URL="$staging_url" uv run alembic upgrade head 2>&1 \
  | tee "$ARTIFACT_DIR/alembic-upgrade.log"
migration_end_ns="$(date +%s%N)"
finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log "auditing and comparing migrated data"
DATABASE_URL="$staging_url" uv run python scripts/postgres_migration_audit.py snapshot \
  --phase post \
  --output "$ARTIFACT_DIR/post.json"
uv run python scripts/postgres_migration_audit.py compare \
  --before "$ARTIFACT_DIR/pre.json" \
  --after "$ARTIFACT_DIR/post.json" \
  --expected-after-revision "$head_revision" \
  --output "$ARTIFACT_DIR/comparison.json"

uv run python - \
  "$ARTIFACT_DIR/timing.json" \
  "$started_at" \
  "$finished_at" \
  "$(( (dump_end_ns - dump_start_ns) / 1000000 ))" \
  "$(( (restore_end_ns - restore_start_ns) / 1000000 ))" \
  "$(( (migration_end_ns - migration_start_ns) / 1000000 ))" <<'PY'
import json
import sys
from pathlib import Path

path, started_at, finished_at, dump_ms, restore_ms, migration_ms = sys.argv[1:]
Path(path).write_text(
    json.dumps(
        {
            "started_at": started_at,
            "finished_at": finished_at,
            "dump_duration_milliseconds": int(dump_ms),
            "restore_duration_milliseconds": int(restore_ms),
            "migration_duration_milliseconds": int(migration_ms),
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

log "rehearsal passed; evidence written to ${ARTIFACT_DIR}"
