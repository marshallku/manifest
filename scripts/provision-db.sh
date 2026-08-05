#!/usr/bin/env bash
#
# provision-db.sh — create (or drop) a per-app database + login role on the
# shared homelab Postgres, and print a ready-to-use DATABASE_URL.
#
# This is the "database.enabled" half of the app factory: one shared Postgres
# instance, one database + one owner role per app (logical isolation), so a new
# app gets its own DB without standing up a new Postgres.
#
# Admin credentials (a superuser / CREATEDB+CREATEROLE role) come from, in order:
#   $DB_ADMIN_URL   = postgres://user:pass@host:port/db   (env, not argv)
#   .env POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB  (+ --host / $DB_HOST)
# Credentials are only taken from the environment / .env — never a CLI flag —
# so no password reaches the process argv. The admin password is passed via
# PGPASSWORD; the generated app password only appears on psql stdin and in the
# printed URL — never in argv or a trace log.
#
# Usage:
#   provision-db.sh <app>                 # create db+role, print DATABASE_URL
#   provision-db.sh <app> --drop          # drop the db+role (teardown)
#   provision-db.sh <app> --host 10.0.0.5 # override Postgres host
#   DB_ADMIN_URL=postgres://postgres:pw@h:5432/postgres provision-db.sh <app>
#
# The printed DATABASE_URL contains the app's password — store it in the app's
# Infisical project (as DATABASE_URL) and add DATABASE_URL to a surface's
# secretEnv in kubernetes/apps/<app>/values.yaml.
set -euo pipefail

# This script handles DB passwords on stdin and PGPASSWORD; running it under
# `set -x` would trace them. Refuse rather than leak.
case $- in *x*) echo "provision-db: refusing to run under 'set -x' (would leak passwords)" >&2; exit 1 ;; esac

DB_HOST="${DB_HOST:-192.168.219.130}"
DB_PORT="${DB_PORT:-5432}"
PSQL_BIN="${PSQL_BIN:-psql}"
command -v "$PSQL_BIN" >/dev/null 2>&1 || PSQL_BIN=/opt/homebrew/opt/libpq/bin/psql

die() { printf 'provision-db: %s\n' "$*" >&2; exit 1; }

# percent-decode a URI component: only %HH per RFC 3986 (in userinfo `+` is a
# literal, unlike form-encoding). printf is a builtin, so no value hits argv.
urldecode() { local s="$1"; printf '%b' "${s//%/\\x}"; }

# read_env_var FILE KEY -> value, WITHOUT executing the file (safe under set -e,
# no set -x / arbitrary code from the file). Non-zero if KEY absent.
read_env_var() {
  local file="$1" key="$2" line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | tail -1)" || return 1
  [[ -n "$line" ]] || return 1
  line="${line#*"${key}="}"; line="${line%$'\r'}"
  if   [[ "$line" == \"*\" ]]; then line="${line#\"}"; line="${line%\"}"
  elif [[ "$line" == \'*\' ]]; then line="${line#\'}"; line="${line%\'}"; fi
  printf '%s' "$line"
}

# --- args -------------------------------------------------------------------
APP=""; ADMIN_URL="${DB_ADMIN_URL:-}"; DROP=0
while (( $# )); do
  case "$1" in
    --drop) DROP=1 ;;
    --host) DB_HOST="$2"; shift ;;
    --port) DB_PORT="$2"; shift ;;
    -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [[ -z "$APP" ]] && APP="$1" || die "unexpected arg: $1" ;;
  esac
  shift
done
[[ -n "$APP" ]] || die "usage: provision-db.sh <app> [--drop] [--host H] [--port P]  (admin creds via \$DB_ADMIN_URL or .env POSTGRES_*)"

# The app name must be a DNS-1123 label (same as its k8s namespace / chart
# `app:`): lowercase alphanumeric + hyphens. Validating the INPUT this way keeps
# the hyphen->underscore mapping one-to-one — otherwise `foo-bar` and `foo_bar`
# would collide onto the same database/role and clobber each other.
[[ "$APP" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
  || die "app name must be a DNS-1123 label (lowercase alphanumeric + hyphens, no underscores): '$APP'"

# postgres identifiers: lower, digits, underscore. hyphens -> underscores.
db_ident="$(printf '%s' "$APP" | tr '-' '_')"
[[ "$db_ident" =~ ^[a-z_][a-z0-9_]*$ ]] || die "app '$APP' -> invalid identifier '$db_ident'"
# Postgres truncates identifiers at 63 bytes (NAMEDATALEN-1); reject rather than
# let the catalog name silently diverge from the emitted URL. db_ident is ASCII
# so bytes == chars here.
(( ${#db_ident} <= 63 )) || die "app '$APP' -> identifier '$db_ident' exceeds Postgres' 63-byte limit"

# --- resolve admin connection (password kept out of argv) -------------------
ADMIN_USER=""; ADMIN_DB=""
if [[ -n "$ADMIN_URL" ]]; then
  # parse URL without printing it
  # Split on the LAST @ so an encoded @ (%40) in the password is fine; then
  # percent-decode each component per RFC 3986.
  proto_removed="${ADMIN_URL#*://}"
  creds="${proto_removed%@*}"; hostpart="${proto_removed##*@}"
  ADMIN_USER="$(urldecode "${creds%%:*}")"; PGADMIN_PW="$(urldecode "${creds#*:}")"
  hostname_port="${hostpart%%/*}"; ADMIN_DB="${hostpart#*/}"; ADMIN_DB="$(urldecode "${ADMIN_DB%%\?*}")"
  DB_HOST="${hostname_port%%:*}"; [[ "$hostname_port" == *:* ]] && DB_PORT="${hostname_port#*:}"
else
  ENVF=""
  for f in "${DB_ENV_FILE:-}" "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env" "$PWD/.env"; do
    [[ -n "$f" && -f "$f" ]] && grep -q '^[[:space:]]*\(export[[:space:]]*\)\?POSTGRES_PASSWORD=' "$f" && { ENVF="$f"; break; }
  done
  [[ -n "$ENVF" ]] || die "no admin creds: set \$DB_ADMIN_URL or provide .env POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB"
  if v="$(read_env_var "$ENVF" POSTGRES_USER || true)"     && [[ -n "$v" ]]; then ADMIN_USER="$v"; fi
  if v="$(read_env_var "$ENVF" POSTGRES_PASSWORD || true)" && [[ -n "$v" ]]; then PGADMIN_PW="$v"; fi
  if v="$(read_env_var "$ENVF" POSTGRES_DB || true)"       && [[ -n "$v" ]]; then ADMIN_DB="$v"; fi
fi
[[ -n "$ADMIN_USER" && -n "${PGADMIN_PW:-}" && -n "$ADMIN_DB" ]] || die "incomplete admin credentials"

# psql as admin against the maintenance db; SQL on stdin (no secrets in argv).
padmin() { PGPASSWORD="$PGADMIN_PW" "$PSQL_BIN" -v ON_ERROR_STOP=1 -X -q \
  -h "$DB_HOST" -p "$DB_PORT" -U "$ADMIN_USER" -d "$ADMIN_DB" "$@"; }

# Ownership guard: the factory only touches objects it created, tagged with a
# COMMENT. This refuses to modify or drop a pre-existing db/role that lacks the
# tag — e.g. the hand-managed sssup / playzy databases sharing this instance —
# so `provision-db.sh sssup` can never clobber maji's database.
FACTORY_MARK='managed-by:app-factory'
obj_state() { # obj_state role|db -> absent | managed | foreign
  case "$1" in
    role) padmin -tAc "SELECT coalesce((SELECT CASE WHEN coalesce(shobj_description(oid,'pg_authid'),'')='$FACTORY_MARK' THEN 'managed' ELSE 'foreign' END FROM pg_roles WHERE rolname='$db_ident'),'absent')" ;;
    # A db is 'managed' if its own comment is tagged OR its owner role is tagged
    # — the latter recovers a db created just before its own COMMENT landed.
    db)   padmin -tAc "SELECT coalesce((SELECT CASE WHEN coalesce(shobj_description(d.oid,'pg_database'),'')='$FACTORY_MARK' OR coalesce(shobj_description(o.oid,'pg_authid'),'')='$FACTORY_MARK' THEN 'managed' ELSE 'foreign' END FROM pg_database d JOIN pg_roles o ON o.oid=d.datdba WHERE d.datname='$db_ident'),'absent')" ;;
  esac
}
guard_not_foreign() {
  local rs ds; rs="$(obj_state role)"; ds="$(obj_state db)"
  if [[ "$rs" == foreign ]]; then die "role '$db_ident' exists but was not created by the factory — refusing to touch it"; fi
  if [[ "$ds" == foreign ]]; then die "database '$db_ident' exists but was not created by the factory — refusing to touch it"; fi
}

# --- teardown ---------------------------------------------------------------
if (( DROP )); then
  guard_not_foreign
  printf 'Drop database %q and role %q on %s? [y/N] ' "$db_ident" "$db_ident" "$DB_HOST" >&2
  read -r ans; [[ "$ans" == [yY] ]] || die "aborted"
  padmin <<SQL
DROP DATABASE IF EXISTS "$db_ident";
DROP ROLE IF EXISTS "$db_ident";
SQL
  echo "dropped $db_ident" >&2
  exit 0
fi

# --- create (idempotent) ----------------------------------------------------
guard_not_foreign   # never mutate a db/role the factory didn't create

# app role password: URL-safe hex, generated locally, never logged.
APP_PW="$(openssl rand -hex 24)"

# role: create-or-reset password AND tag it, atomically in one transaction, so
# an interruption leaves either a fully-tagged role or none (never an untagged
# orphan the ownership guard would later reject).
padmin <<SQL
BEGIN;
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$db_ident') THEN
    CREATE ROLE "$db_ident" LOGIN PASSWORD '$APP_PW';
  ELSE
    ALTER ROLE "$db_ident" WITH LOGIN PASSWORD '$APP_PW';
  END IF;
END
\$\$;
COMMENT ON ROLE "$db_ident" IS '$FACTORY_MARK';
COMMIT;
SQL

# database: CREATE DATABASE cannot run in a DO/transaction — use \gexec.
padmin <<SQL
SELECT 'CREATE DATABASE "$db_ident" OWNER "$db_ident"'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$db_ident')\gexec
SQL

# ensure ownership + privileges and tag the database (the role is already tagged
# above; the db's owner being that tagged role is what makes this step's own
# interruption recoverable — see obj_state db).
padmin <<SQL
ALTER DATABASE "$db_ident" OWNER TO "$db_ident";
GRANT ALL PRIVILEGES ON DATABASE "$db_ident" TO "$db_ident";
COMMENT ON DATABASE "$db_ident" IS '$FACTORY_MARK';
SQL

echo "provisioned database '$db_ident' + role '$db_ident' on ${DB_HOST}:${DB_PORT}" >&2
echo "add this to the app's Infisical project as DATABASE_URL (and to a surface's secretEnv):" >&2
printf 'DATABASE_URL=postgres://%s:%s@%s:%s/%s?sslmode=disable\n' \
  "$db_ident" "$APP_PW" "$DB_HOST" "$DB_PORT" "$db_ident"
