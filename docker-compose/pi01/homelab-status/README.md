# homelab-status

Collapses Uptime Kuma and Prometheus into a single ~1 KB JSON document at
`http://192.168.219.127:8080/homelab.json`.

The consumer is the ESP32-S3 shelf display in
[marshallku/esp32](https://github.com/marshallku/esp32) (`homelab-display`): a
400×300 monochrome panel with no clock, no filesystem and a fixed-size JSON
parser. Anything that would be awkward there is done here instead.

## Why this runs on pi01

Same reasoning as `../uptime-kuma`. Kuma's SQLite database is read **locally**,
so when `prd01` and the whole k3s cluster are gone the display still names the
things that are down — which is the only moment it earns its shelf space. A
copy of this stack on `prd01` would go dark in exactly that scenario.

Prometheus does not get the same treatment: it lives in-cluster, so it dies
with `prd01`. That is reported rather than hidden — `hosts.ok` goes `false`
with an `error` string, the `kuma` half stays intact, and the device blanks the
host panel instead of showing stale gauges.

## Output

```jsonc
{
  "v": 1,
  "generated": 1788479374,          // unix seconds, when the snapshot was built
  "generated_at": "09-04 10:35:35", // the same moment, preformatted for display
  "age": 12,                        // seconds, stamped at *request* time
  "stale": false,           // age > STALE_AFTER_SEC
  "ready": true,            // false until the first refresh lands
  "kuma": {
    "ok": true,
    "up": 54, "total": 56,
    "unsettled": 2,           // PENDING or MAINTENANCE, outside the ratio
    "groups": [{ "label": "HOSTS", "up": 8, "total": 8 }],
    "down": [{ "name": "dongjoo.me", "secs": 1683208 }],
    "down_more": 0          // down monitors beyond the MAX_DOWN cap
  },
  "hosts": {
    "ok": true,
    "nodes": [
      { "name": "prd01", "cpu": 5.9, "mem": 24.6, "disk": 58.4, "load": 0.7, "up_d": 17.5 }
    ]
  }
}
```

`age` is computed when the request is served, not when the snapshot was built,
so a wedged refresh thread shows up as a growing age rather than as silence. It
is what `stale` is derived from.

`generated_at` is that same moment as a wall-clock string, formatted here
because the device has no synchronised clock. **It, not `age`, is what the
display shows**, and the reason is a failure an age cannot catch: an age is
something the device asserts about itself, so firmware that wedges just after a
good draw keeps showing "5s ago" and looks healthy forever. A wall-clock stamp
is a value the device merely echoes — a frozen screen stops agreeing with the
clock on the wall, which anyone can check against a wristwatch without trusting
the device at all.

The date is always included. Without it, a day-old frozen screen would be
indistinguishable from a fresh one.

Only settled states count, and they leave the ratio **entirely** — numerator
and denominator both. Keeping a `PENDING` monitor in the denominator is what
would make the headline flap: a group at `8/8` would read `7/8` for the length
of every retry window and look like an outage. Excluding it keeps that group at
`7/7`.

Silently shrinking the denominator would be its own kind of lie, so the
excluded ones are reported as `unsettled` and the display prints them in the
footer. `up + down_listed + unsettled` accounts for every active monitor.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `STATUS_TOKEN` | *(required)* | Bearer token the display must present; min 16 chars. Empty refuses to start |
| `STATUS_ALLOW_ANONYMOUS` | unset | `1` to deliberately serve without auth. Warns on every start |
| `KUMA_DB` | `/kuma/kuma.db` | Kuma SQLite database, mounted read-only |
| `PROM_URL` | `http://192.168.219.100:30090` | Prometheus NodePort on prd01 |
| `REFRESH_SEC` | `30` | Snapshot rebuild interval |
| `PROM_TIMEOUT` | `4` | Per-query timeout, seconds |
| `STALE_AFTER_SEC` | `180` | Age at which `stale` flips true |
| `PORT` / `LISTEN` | `8080` / `0.0.0.0` | HTTP bind |

Screen budgets (`MAX_GROUPS`, `MAX_DOWN`, `MAX_HOSTS`, `MAX_NAME_LEN`) are
constants in `aggregate.py` — they exist to keep the firmware's fixed-size
buffers honest, so change them in both places or not at all.

## Group labels

The firmware draws with `embedded-graphics`' built-in ASCII fonts, which cannot
render Kuma's Korean group names. `GROUP_LABELS` in `aggregate.py` maps them to
short ASCII labels:

| Kuma group | Label |
| --- | --- |
| 호스트 | `HOSTS` |
| 인프라 코어 | `CORE` |
| 관측 스택 | `OBSERV` |
| GitOps · 시크릿 | `GITOPS` |
| 공개 서비스 | `PUBLIC` |
| 내부 오리진 (prd01) | `ORIGIN` |
| k8s 앱 (NodePort) | `K8S APPS` |
| 데이터스토어 | `DATA` |

An unmapped group is not dropped — it falls back to an ASCII-stripped,
uppercased form of its own name. **Renaming a group in Kuma silently changes
its label on the display**, so add the new name here when that happens.

Groups are emitted in Kuma's own creation order rather than sorted by health;
a panel that reshuffles the moment something breaks is hardest to read exactly
when it matters.

## Authentication

The endpoint requires `Authorization: Bearer <STATUS_TOKEN>`; anything else
gets a 401. Comparison is constant-time.

**It fails closed.** With no token and no explicit `STATUS_ALLOW_ANONYMOUS=1`,
the container exits at startup instead of serving. `.env.example` ships the
token blank, so the alternative was a stack that comes up looking healthy while
quietly serving the LAN — a log line nobody reads is not a safeguard.

This is not ceremony. The document is a *subset of what Uptime Kuma keeps
behind a login* — monitor names spell out internal hosts, ports and the service
inventory (`Postgres (db01:5432)`, `MongoDB blog (prd01:27019)`, and so on).
Serving that openly on the LAN would make the summary weaker than the thing it
summarises, which is a strange place to end up.

The same token is baked into the firmware at build time from the esp32 repo's
own `.env` (`STATUS_TOKEN`), the way `scd41-monitor` already handles its
InfluxDB token. Rotating it means editing both `.env` files, restarting this
stack, and reflashing the board.

What the token does **not** protect: the container can read all of `kuma.db`,
not just the columns this script selects. Kuma's own `/metrics` endpoint is
API-key gated, but the database file is world-readable on pi01, so reading it
directly sidesteps that gate. Today the database holds one push token, the
bcrypt user hash, two hashed API keys and one notification channel config. The
script only ever selects monitor ids, names, types, parents and heartbeat
status/time — but the *access* is broader than the use, and that is worth
knowing before adding anything else to this container.

## Setup

```sh
cd ~/dev/manifest/docker-compose/pi01/homelab-status
cp .env.example .env && chmod 600 .env
$EDITOR .env                # STATUS_TOKEN=$(openssl rand -hex 24)
docker compose up -d

curl -s -H "Authorization: Bearer $STATUS_TOKEN" \
  http://127.0.0.1:8080/homelab.json | python3 -m json.tool
```

No build step — `aggregate.py` is standard library only and is bind-mounted, so
a change to it needs `docker compose restart`, not a rebuild.

## Notes

- **SQLite is read in place over Kuma's live WAL** (`mode=ro` plus
  `PRAGMA query_only`). Copying the database first was the obvious approach and
  is the wrong one here: ~34 MB per refresh against an SD card whose write
  volume `../README.md` deliberately budgets, for data already in the page
  cache.
- Kuma stores heartbeat timestamps as **naive UTC**, despite `TZ=Asia/Seoul` in
  its own compose file — that variable only affects how its web UI renders
  them. `secs` (down duration) is computed against UTC accordingly.
- **Every response closes its connection.** The display's TCP stack resets the
  socket rather than closing it politely, so with keep-alive on, the server sat
  in `readline` until that reset and `socketserver` printed a full traceback —
  one per poll, every 30 s, forever. `Connection: close` plus a `handle_error`
  override that swallows `ConnectionResetError`/`BrokenPipeError` brings the
  steady-state log to zero lines. Verified: 0 new lines over 75 s of live
  polling.
- Worth adding a Kuma monitor for this endpoint. Nothing else watches it, and a
  display frozen on old data is worse than a blank one. It would need the bearer
  token in a custom header, or a `port` monitor on 8080 as a cruder stand-in.
- The container's healthcheck presents the token too, so `docker ps` health is a
  real end-to-end check rather than a liveness ping.
