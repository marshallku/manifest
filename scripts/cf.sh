#!/usr/bin/env bash
#
# cf.sh — thin Cloudflare CLI for the homelab app factory.
#
# Control DNS + tunnels from the terminal instead of the Zero Trust dashboard.
# The tunnel-ingress controller manages records automatically; this wrapper is
# for inspecting what exists, verifying credentials, and cleaning up orphans.
#
# Config (env, with sensible marshallku.dev defaults for account/zone):
#   CF_TOKEN        required — API token (Tunnel:Edit, DNS:Edit, Zone:Read)
#   CF_ACCOUNT_ID   default c6cefaad66790caf21356b0c4d82ed34
#   CF_ZONE_ID      default 4781928e6b4471ccc10c12eda95dbff7 (marshallku.dev)
#   CF_ENV_FILE     optional path to an env file to source for the above
#
# The first of $CF_ENV_FILE / <repo>/.env / ./.env that defines CF_TOKEN is
# sourced (token is never printed, even under `bash -x`). Per-variable
# precedence: that env file > the current environment > the built-in defaults.
#
# Usage:
#   cf.sh verify                 # validate the token
#   cf.sh zone                   # show the zone
#   cf.sh dns list [substr]      # list DNS records (optionally name-filtered)
#   cf.sh dns get <name>         # show one record as JSON
#   cf.sh dns delete <name>      # delete record(s) by exact name (asks first)
#   cf.sh tunnel list            # list tunnels
#   cf.sh tunnel info <name>     # tunnel detail + active connections
#   cf.sh tunnel routes <name>   # published hostname routes of a tunnel
set -euo pipefail

# Defaults are applied AFTER the env file is sourced (see init), so account/zone
# set in the environment or $CF_ENV_FILE/.env win over these fallbacks.
CF_ACCOUNT_ID_DEFAULT="c6cefaad66790caf21356b0c4d82ed34"
CF_ZONE_ID_DEFAULT="4781928e6b4471ccc10c12eda95dbff7"
API="https://api.cloudflare.com/client/v4"

die() { printf 'cf: %s\n' "$*" >&2; exit 1; }

# URL-encode a value for safe use in a query string (names may contain spaces
# or &, %, # ...).
urlenc() { jq -rn --arg s "$1" '$s|@uri'; }

# --- load CF_TOKEN without echoing it ----------------------------------------
# xtrace is disabled for the whole function, BEFORE any $CF_TOKEN expansion or
# env-file sourcing, so the secret never reaches a trace log even under `bash -x`
# (or a sourced env file that turns tracing on). Prior xtrace state is restored.
# read_env_var FILE KEY -> the value of KEY in an env file, WITHOUT executing
# the file (so a bad line can't trip `set -e`, and `set -x`/arbitrary code in
# the file can't run or leak). Strips `export`, one layer of matching quotes,
# and a trailing CR. Returns non-zero if KEY is absent.
read_env_var() {
  local file="$1" key="$2" line
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | tail -1)" || return 1
  [[ -n "$line" ]] || return 1
  line="${line#*"${key}="}"
  line="${line%$'\r'}"
  if   [[ "$line" == \"*\" ]]; then line="${line#\"}"; line="${line%\"}"
  elif [[ "$line" == \'*\' ]]; then line="${line#\'}"; line="${line%\'}"; fi
  printf '%s' "$line"
}

# Load CF_TOKEN (+ optional account/zone) from the first candidate env file that
# defines CF_TOKEN, by PARSING not sourcing. xtrace is off for the whole
# function so no value reaches a trace log. Per-variable precedence:
# env file > current environment > built-in default.
load_token() {
  local xt=0; case $- in *x*) xt=1; set +x;; esac
  local candidates=() f v
  [[ -n "${CF_ENV_FILE:-}" ]] && candidates+=("$CF_ENV_FILE")
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  candidates+=("$here/.env" "$PWD/.env")
  for f in "${candidates[@]}"; do
    if [[ -f "$f" ]] && grep -q '^[[:space:]]*\(export[[:space:]]*\)\?CF_TOKEN=' "$f"; then
      # `if` blocks keep the (possibly-failing) reads out of set -e's reach; the
      # `|| true` stops an absent key's non-zero from aborting under set -e.
      if v="$(read_env_var "$f" CF_TOKEN || true)"      && [[ -n "$v" ]]; then CF_TOKEN="$v"; fi
      if v="$(read_env_var "$f" CF_ACCOUNT_ID || true)" && [[ -n "$v" ]]; then CF_ACCOUNT_ID="$v"; fi
      if v="$(read_env_var "$f" CF_ZONE_ID || true)"    && [[ -n "$v" ]]; then CF_ZONE_ID="$v"; fi
      break
    fi
  done
  if [[ -z "${CF_TOKEN:-}" ]]; then
    if (( xt )); then set -x; fi
    die "CF_TOKEN not set (export it or put it in .env / \$CF_ENV_FILE)"
  fi
  if (( xt )); then set -x; fi
}

# init = load the token, then fall back to the built-in account/zone only if
# neither the environment nor a sourced env file supplied them.
init() {
  load_token
  CF_ACCOUNT_ID="${CF_ACCOUNT_ID:-$CF_ACCOUNT_ID_DEFAULT}"
  CF_ZONE_ID="${CF_ZONE_ID:-$CF_ZONE_ID_DEFAULT}"
}

# api METHOD PATH -> raw JSON; fails loudly on success:false.
# xtrace is disabled around the curl call so the bearer token never lands in a
# trace log, even under `bash -x` (which would otherwise print the header).
api() {
  local method="$1" path="$2"; shift 2
  local resp xt=0; case $- in *x*) xt=1; set +x;; esac
  resp="$(curl -sS -X "$method" "${API}${path}" \
    -H "Authorization: Bearer ${CF_TOKEN}" \
    -H "Content-Type: application/json" "$@")"
  if (( xt )); then set -x; fi
  if [[ "$(jq -r '.success' <<<"$resp")" != "true" ]]; then
    jq -r '.errors' <<<"$resp" >&2
    die "API $method $path failed"
  fi
  printf '%s' "$resp"
}

# api_all PATH -> merged JSON array of every page's .result. Follows
# result_info.total_pages so listings are complete, not capped at one page.
api_all() {
  local path="$1" page=1 total=1 resp results='[]'
  local sep='?'; [[ "$path" == *'?'* ]] && sep='&'
  while (( page <= total )); do
    resp="$(api GET "${path}${sep}per_page=100&page=${page}")"
    total="$(jq -r '.result_info.total_pages // 1' <<<"$resp")"
    results="$(jq -c --argjson acc "$results" '$acc + .result' <<<"$resp")"
    page=$((page + 1))
  done
  printf '%s' "$results"
}

need() { [[ -n "${1:-}" ]] || die "$2"; }

# --- commands ---------------------------------------------------------------
cmd_verify() {
  api GET "/user/tokens/verify" | jq -r '.result | "token \(.id): \(.status)"'
}

cmd_zone() {
  api GET "/zones/${CF_ZONE_ID}" | jq -r '.result | "\(.name)  (id \(.id), status \(.status))"'
}

cmd_dns() {
  local sub="${1:-list}"; shift || true
  case "$sub" in
    list)
      local filter="${1:-}"
      api_all "/zones/${CF_ZONE_ID}/dns_records" \
        | jq -r --arg f "$filter" '
            map(select($f == "" or (.name | contains($f))))
            | sort_by(.name)[]
            | "\(.type)\t\(.name)\t-> \(.content)\(if .proxied then "  (proxied)" else "" end)"' \
        | column -t -s $'\t'
      ;;
    get)
      need "${1:-}" "usage: cf.sh dns get <name>"
      api GET "/zones/${CF_ZONE_ID}/dns_records?name=$(urlenc "$1")" | jq '.result'
      ;;
    delete)
      need "${1:-}" "usage: cf.sh dns delete <name>"
      local name="$1" ids
      ids="$(api GET "/zones/${CF_ZONE_ID}/dns_records?name=$(urlenc "$name")" | jq -r '.result[].id')"
      [[ -n "$ids" ]] || die "no DNS record named $name"
      printf 'Delete %d record(s) for %s? [y/N] ' "$(wc -w <<<"$ids")" "$name"
      read -r ans; [[ "$ans" == "y" || "$ans" == "Y" ]] || die "aborted"
      local id
      for id in $ids; do
        api DELETE "/zones/${CF_ZONE_ID}/dns_records/${id}" >/dev/null
        echo "deleted $id"
      done
      ;;
    *) die "unknown dns subcommand: $sub" ;;
  esac
}

# resolve a tunnel name -> id
tunnel_id() {
  api GET "/accounts/${CF_ACCOUNT_ID}/cfd_tunnel?name=$(urlenc "$1")&is_deleted=false" \
    | jq -r '.result[0].id // empty'
}

cmd_tunnel() {
  local sub="${1:-list}"; shift || true
  case "$sub" in
    list)
      api_all "/accounts/${CF_ACCOUNT_ID}/cfd_tunnel?is_deleted=false" \
        | jq -r '.[] | "\(.name)\t\(.id)\t\(.status)"' | column -t -s $'\t'
      ;;
    info)
      need "${1:-}" "usage: cf.sh tunnel info <name>"
      local id; id="$(tunnel_id "$1")"; [[ -n "$id" ]] || die "no tunnel named $1"
      api GET "/accounts/${CF_ACCOUNT_ID}/cfd_tunnel/${id}" \
        | jq '.result | {name, id, status, created_at, connections: (.connections | length)}'
      ;;
    routes)
      need "${1:-}" "usage: cf.sh tunnel routes <name>"
      local id; id="$(tunnel_id "$1")"; [[ -n "$id" ]] || die "no tunnel named $1"
      api GET "/accounts/${CF_ACCOUNT_ID}/cfd_tunnel/${id}/configurations" \
        | jq -r '.result.config.ingress[]? | "\(.hostname // "*")\t-> \(.service)"' \
        | column -t -s $'\t'
      ;;
    *) die "unknown tunnel subcommand: $sub" ;;
  esac
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    verify) init; cmd_verify ;;
    zone)   init; cmd_zone ;;
    dns)    init; cmd_dns "$@" ;;
    tunnel) init; cmd_tunnel "$@" ;;
    ""|-h|--help|help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      ;;
    *) die "unknown command: $cmd (try: cf.sh help)" ;;
  esac
}

main "$@"
