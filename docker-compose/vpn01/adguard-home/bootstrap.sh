#!/usr/bin/env bash
#
# One-time bootstrap for the vpn01 AdGuard Home replica.
#
# Adapted from ../../pi01/adguard-home/bootstrap.sh — same install-API approach,
# two deliberate differences (see DNS_BIND and set_upstreams below).
#
# Why this instance exists at all: vpn01 is the Tailscale exit node whose egress
# rides the AdGuard VPN tunnel. Tailscale sends an exit-node client's DNS to the
# exit node's *system resolver*, so whatever vpn01 resolves with is what the phone
# resolves with. Pointing that at the LAN AdGuard (app01) keeps filtering but
# leaves DNS egressing over the home ISP — which is exactly the leg that domestic
# DNS blocking and injection act on. A local instance whose upstream is routed
# through the tunnel closes that gap while keeping the same filter lists.
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

# Unlike pi01, this instance binds loopback only, NOT 0.0.0.0.
#
# systemd-resolved's stub already owns 127.0.0.53:53 and must keep it: tailscaled
# hands exit-node client queries to the system resolver, which is that stub. Two
# distinct loopback addresses coexist on :53 without conflict, so AdGuard takes
# 127.0.0.1 and resolved forwards to it. Binding 0.0.0.0 here would collide with
# the stub and would also expose an open resolver on the LAN and the tailnet,
# which this box has no reason to be.
readonly DNS_BIND=127.0.0.1

# Same resolver app01 uses, so answers match what the house sees — only the
# egress point differs. These leave the box through tun0 because adguardvpn's
# routing table 880 covers the public internet; before the tunnel is up they
# still work over the home link, which is what lets the tunnel bootstrap itself.
readonly UPSTREAM_DNS_JSON='["https://cloudflare-dns.com/dns-query"]'
readonly BOOTSTRAP_DNS_JSON='["1.1.1.1","1.0.0.1"]' 

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
    # Reject newlines HERE, before the value is printed. The shell side receives
    # these one per line and reads them with `read`, which splits on exactly this
    # character — so by the time the value reaches bash the newline is already
    # gone and a multiline password would be silently truncated to its first line.
    # AdGuard would then be installed with the truncated value while the sync
    # container is handed the original, surfacing much later as a 401.
    if "\n" in value or "\r" in value:
        sys.exit(f"error: {key} contains a newline — not supported")
    # `compose config` re-escapes a literal $ as $$ so its output stays a valid
    # compose file. Undo that: what the container is handed is the single-$ form,
    # and installing AdGuard with the doubled one would mean the sync job could
    # never authenticate against the instance it just set up.
    print(value.replace("$$", "$"))
')
# `IFS=` 가 없으면 read 가 앞뒤 공백을 먹는다. .env 에 `PASSWORD=" hunter2 "` 처럼
# 공백을 포함한 값을 넣으면 AdGuard 는 `hunter2` 로 설치되는데 sync 컨테이너는
# 원본 값을 받으므로, 나중에 401 로만 드러나는 어긋남이 생긴다.
# (pi01 원본에도 있는 버그다 — 그쪽은 이미 설치돼 있어 건드리지 않았다.)
{
    IFS= read -r REPLICA_USERNAME
    IFS= read -r REPLICA_PASSWORD
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
    "dns": {"ip": sys.argv[3], "port": int(sys.argv[2])},
    "username": os.environ["REPLICA_USERNAME"],
    "password": os.environ["REPLICA_PASSWORD"],
}, sys.stdout)' "$WEB_PORT" "$DNS_PORT" "$DNS_BIND" |
        curl --connect-timeout 3 --max-time 30 -sf -X POST \
            -H 'Content-Type: application/json' --data @- \
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

# Applied on every run, and deliberately NOT replicated from the origin: the sync
# job runs with --feature-dns-server-config=false precisely so this survives.
# If the origin's upstream (a plain LAN-reachable resolver from app01's point of
# view) were copied here, DNS would go back out over the home link and this whole
# instance would be pointless.
set_upstreams() {
    echo "==> pinning upstreams to ${UPSTREAM_DNS_JSON} (routed through the VPN tunnel)"
    # Goes through api(), which passes the body in argv. Piping it on stdin as
    # `--data @-` looks tidier but silently sends an EMPTY body: curl_auth already
    # consumes stdin for `-K -`, and AdGuard answers 415 "empty body with
    # content-type application/json not allowed". The install call above can use
    # stdin only because that endpoint is unauthenticated. argv is acceptable
    # here — this payload is configuration, not a credential.
    api POST /control/dns_config \
        "{\"upstream_dns\":${UPSTREAM_DNS_JSON},\"bootstrap_dns\":${BOOTSTRAP_DNS_JSON}}"
}

# Credentials go in via curl's stdin config rather than -u, because argv is
# world-readable through /proc/<pid>/cmdline. Quotes and backslashes are escaped
# so a password containing either cannot break the config syntax.
# 마감시한을 여기서 한 번에 건다. 이 홈랩에서 실제로 겪은 실패 모드가 "포트는 열려
# 있는데 아무도 응답하지 않는다"(docker-proxy 가 리스너 없는 포트를 붙잡고 있던 건)
# 라서, 타임아웃 없는 probe 는 30회 루프가 있어도 첫 시도에서 영원히 멈춘다.
curl_auth() {
    local user=${REPLICA_USERNAME//\\/\\\\} pass=${REPLICA_PASSWORD//\\/\\\\}
    printf 'user = "%s:%s"\n' "${user//\"/\\\"}" "${pass//\"/\\\"}" \
        | curl --connect-timeout 3 --max-time 15 -K - "$@"
}

# AdGuard is configured: the UI has moved to WEB_PORT and now demands auth.
serving_ui() {
    curl_auth -sf -o /dev/null "http://127.0.0.1:${WEB_PORT}/control/status"
}

# AdGuard has no config yet and is serving the first-run wizard on SETUP_PORT.
serving_wizard() {
    curl --connect-timeout 3 --max-time 15 -sf -o /dev/null "http://127.0.0.1:${SETUP_PORT}/control/install/get_addresses"
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
set_upstreams

echo "done — DNS on ${DNS_BIND}:${DNS_PORT}, UI on :${WEB_PORT}"
echo "next: fill ORIGIN_* in .env, then \`docker compose up -d\` to start the sync job"
echo "then : re-run infra/bootstrap/vpn01.sh — it points systemd-resolved here"
