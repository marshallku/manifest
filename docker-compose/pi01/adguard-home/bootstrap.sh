#!/usr/bin/env bash
#
# One-time bootstrap for the pi01 AdGuard Home replica.
#
# AdGuard's on-disk config schema moves between releases, so instead of
# committing a hand-written AdGuardHome.yaml we drive the first-run install API
# and let AdGuard author its own. Re-running is safe: the script exits early
# once the instance reports itself already configured.

set -euo pipefail

cd "$(dirname "$0")"

readonly SETUP_PORT=3000 # AdGuard serves the install wizard here until configured
readonly WEB_PORT=3080   # ...and moves the UI here once it is
readonly DNS_PORT=53
readonly DATA_DIR=/var/lib/homelab/adguard-home

# Retention windows, in milliseconds — the unit the AdGuard API actually uses.
# AdGuard installs with a 90-day query log, which is the single largest source of
# steady writes on this host. The replica only needs enough history to debug a
# failover, and the primary keeps the long record.
readonly QUERYLOG_INTERVAL_MS=86400000 # 24h
readonly STATS_INTERVAL_MS=86400000    # 24h

if [[ ! -f .env ]]; then
    echo "error: .env not found — copy .env.example and fill it in" >&2
    exit 1
fi

# Credentials are read back out of `docker compose config` rather than by
# sourcing .env as shell. Sourcing would expand `$`, backticks and quotes, while
# compose passes the same file to the container literally — so a password
# containing shell syntax would install AdGuard with different credentials than
# the sync container is handed, surfacing later as a 401 that is tedious to
# trace (and, with `$(...)`, would execute before that). Going through compose
# leaves exactly one interpretation of the file.
creds=$(docker compose config --format json </dev/null | python3 -c '
import json, sys
env = json.load(sys.stdin)["services"]["adguardhome-sync"]["environment"]
for key in ("REPLICA_USERNAME", "REPLICA_PASSWORD"):
    value = env.get(key, "")
    if not value:
        sys.exit(f"error: {key} is empty — fill it in .env")
    # `compose config` re-escapes a literal $ as $$ so its output stays a valid
    # compose file. Undo that: what the container is handed is the single-$ form,
    # and installing AdGuard with the doubled one would mean the sync job could
    # never authenticate against the instance it just set up.
    print(value.replace("$$", "$"))
')
{
    read -r REPLICA_USERNAME
    read -r REPLICA_PASSWORD
} <<<"$creds"
export REPLICA_USERNAME REPLICA_PASSWORD

install_adguard() {
    echo "==> running install API"
    # JSON is assembled by python3 so a password containing quotes or backslashes
    # cannot break out of the body, and piped straight into curl so it never
    # lands in argv. The install endpoint is unauthenticated, so stdin is free
    # here — unlike the calls below, which need it for credentials.
    python3 -c '
import json, os, sys
json.dump({
    "web": {"ip": "0.0.0.0", "port": int(sys.argv[1])},
    "dns": {"ip": "0.0.0.0", "port": int(sys.argv[2])},
    "username": os.environ["REPLICA_USERNAME"],
    "password": os.environ["REPLICA_PASSWORD"],
}, sys.stdout)' "$WEB_PORT" "$DNS_PORT" |
        curl -sf -X POST -H 'Content-Type: application/json' --data @- \
            "http://127.0.0.1:${SETUP_PORT}/control/install/configure" >/dev/null

    echo "==> waiting for the UI to move to :${WEB_PORT}"
    for _ in $(seq 30); do
        serving_ui && return 0
        sleep 1
    done
    echo "error: AdGuard never came up on :${WEB_PORT} after install" >&2
    exit 1
}

# Applied on every run, not just the first: an AdGuard upgrade or a hand-edit in
# the UI can put the 90-day default back, and nothing else guards this.
tighten_retention() {
    echo "==> setting query log and statistics retention to 24h"
    api PUT /control/querylog/config/update \
        "{\"enabled\":true,\"interval\":${QUERYLOG_INTERVAL_MS},\"anonymize_client_ip\":false,\"ignored\":[]}"
    api PUT /control/stats/config/update \
        "{\"enabled\":true,\"interval\":${STATS_INTERVAL_MS},\"ignored\":[]}"
}

# Credentials go in via curl's stdin config rather than -u, because argv is
# world-readable through /proc/<pid>/cmdline. Quotes and backslashes are escaped
# so a password containing either cannot break the config syntax.
curl_auth() {
    local user=${REPLICA_USERNAME//\\/\\\\} pass=${REPLICA_PASSWORD//\\/\\\\}
    printf 'user = "%s:%s"\n' "${user//\"/\\\"}" "${pass//\"/\\\"}" | curl -K - "$@"
}

# AdGuard is configured: the UI has moved to WEB_PORT and now demands auth.
serving_ui() {
    curl_auth -sf -o /dev/null "http://127.0.0.1:${WEB_PORT}/control/status"
}

# AdGuard has no config yet and is serving the first-run wizard on SETUP_PORT.
serving_wizard() {
    curl -sf -o /dev/null "http://127.0.0.1:${SETUP_PORT}/control/install/get_addresses"
}

api() {
    curl_auth -sf -X "$1" -H 'Content-Type: application/json' --data "$3" \
        "http://127.0.0.1:${WEB_PORT}$2" >/dev/null
}

echo "==> preparing ${DATA_DIR}"
sudo mkdir -p "${DATA_DIR}/work" "${DATA_DIR}/conf"

# Start unconditionally, before deciding anything. Probing first would misread a
# configured-but-stopped container as "not installed" and then wait forever for a
# first-run wizard that will never appear.
echo "==> starting AdGuard"
docker compose up -d adguardhome

echo "==> detecting install state"
for _ in $(seq 30); do
    if serving_ui; then
        state=installed
        break
    elif serving_wizard; then
        state=fresh
        break
    fi
    sleep 1
done

case "${state:-}" in
    installed) echo "==> already installed on :${WEB_PORT}, skipping install" ;;
    fresh) install_adguard ;;
    *)
        echo "error: AdGuard answered on neither :${SETUP_PORT} nor :${WEB_PORT}" >&2
        echo "       check \`docker compose logs adguardhome\`; if the credentials in" >&2
        echo "       .env no longer match the instance, :${WEB_PORT} will 401." >&2
        exit 1
        ;;
esac

tighten_retention

echo "done — DNS on :${DNS_PORT}, UI on :${WEB_PORT}"
echo "next: fill ORIGIN_* in .env, then \`docker compose up -d\` to start the sync job"
