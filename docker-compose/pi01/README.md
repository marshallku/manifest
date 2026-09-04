# pi01 — Raspberry Pi 4B (192.168.219.127)

Debian 13 (trixie), **arm64**, 4 cores / 3.7 GiB RAM, 57 GB SD card.

This host exists to hold the pieces that are only useful when `prd01` is *down*.
`prd01` is simultaneously the k3s control-plane and the Docker host for every
stack in the flat `docker-compose/` layout — including the AdGuard Home that
serves DNS for the whole LAN. Losing it currently takes name resolution and any
hope of an alert with it.

So everything here is **outside the k3s cluster** and shares no dependency with
it. Nothing on pi01 should ever need `prd01`, the cluster API, or the HDD to
start.

| Stack | Port | Role |
| --- | --- | --- |
| `adguard-home` | 53, 3080 | Secondary DNS; config replicated from the primary |
| `uptime-kuma` | 3001 | External probe + the homelab's only alerting path |
| `node-exporter` | 9100 | Metrics, scraped over the LAN by the in-cluster Prometheus |
| `homelab-status` | 8080 | One ~1 KB JSON rollup of Kuma + Prometheus, for the ESP32 shelf display |

Images must be multi-arch — this host is arm64 while every cluster node is
amd64. All five images in use here publish arm64.

## Storage

Everything is on the SD card under `/var/lib/homelab/<app>`, **not** `/mnt/hdd`.
See `../README.md` for the reasoning; briefly, `/mnt/hdd` is exFAT (no POSIX
permissions — `chmod 600` silently stays `755`) on a drive reporting 90
reallocated sectors, so it is unfit to carry the thing that has to survive a
failure.

SD write volume is bounded by configuration instead:

- AdGuard query log and statistics retention are cut to **24h** by
  `adguard-home/bootstrap.sh` (AdGuard installs with a 90-day query log).
- `/etc/docker/daemon.json` already caps container logs at `10m x 3`.
- Uptime Kuma: set *Settings → Monitor History → Keep data for* to **30 days**
  after first login. It defaults to 180 and writes a heartbeat row per monitor
  per interval.
- `homelab-status` writes nothing at all: it reads Kuma's database read-only,
  in place over the live WAL, and keeps its output in memory. Copying the
  database each refresh would have cost ~34 MB of writes every 30 s.

> The `uptime-kuma` compose file pins `1.23.17`, but the running container is
> **2.5.0**. Reconcile that before relying on the pinned tag; `homelab-status`
> reads the database directly and works with either, but the status-page HTTP
> API differs between the two majors.

## Setup

```sh
git clone https://github.com/marshallku/manifest.git ~/dev/manifest
cd ~/dev/manifest/docker-compose/pi01

# 1. AdGuard replica
cd adguard-home
cp .env.example .env && chmod 600 .env
$EDITOR .env                # fill ORIGIN_* (primary) and REPLICA_* (this host)
./bootstrap.sh              # installs AdGuard via its own install API
docker compose up -d        # starts the replica + the sync job

# 2. metrics and probing
cd ../node-exporter && docker compose up -d
cd ../uptime-kuma  && docker compose up -d

# 3. status rollup for the ESP32 display (needs uptime-kuma running first)
cd ../homelab-status
cp .env.example .env && chmod 600 .env
$EDITOR .env                # STATUS_TOKEN — must match the firmware's own .env
docker compose up -d
```

`bootstrap.sh` is idempotent — re-running it skips the install and just
re-applies the retention limits, which is worth doing after an AdGuard upgrade.

## Router configuration

Two manual steps on the router; neither can be done from this repo.

1. **DHCP reservation** for pi01's `eth0` MAC → `192.168.219.127`. The address
   is currently a plain DHCP lease (`ipv4.method: auto`), and a backup DNS
   server that can change address is not a backup. The Prometheus target and
   the `REPLICA_URL` in `.env` both hardcode it.
2. **Secondary DNS** in the DHCP settings:
   `DNS1 = 192.168.219.100`, `DNS2 = 192.168.219.127`.

> pi01 is dual-homed — `eth0` at `.127` and `wlan0` at `.106`, both DHCP.
> AdGuard binds `0.0.0.0`, so it answers on either, but only `.127` is pinned
> and advertised. Consider disabling `wlan0` to remove the ambiguity.

Be aware of what a secondary DNS entry does and does not buy you: resolvers pick
between the two on their own schedule, and most do not fail over instantly.
Expect a few seconds of failed lookups at the moment `prd01` dies, and expect a
share of everyday queries to land here even while the primary is healthy. That
second part is a feature — it is what proves the replica still works.

## Uptime Kuma monitors

Kuma has no config-as-code, so these are created in the UI. The point is to
watch things *from outside* the cluster:

| Monitor | Type | Target |
| --- | --- | --- |
| prd01 host | Ping | 192.168.219.100 |
| k3s API | TCP Port | 192.168.219.100:6443 |
| Primary DNS | DNS | resolve `example.com` via 192.168.219.100 |
| ArgoCD / Grafana | HTTP(s) | the in-cluster ingress URLs |
| Public site | HTTP(s) | through Cloudflare, to catch tunnel failures |

Add a notification channel first (Settings → Notifications) — without one the
probes are just a dashboard nobody is looking at when it matters.

## Verifying failover

Worth doing once, deliberately, rather than discovering it during a real outage:

```sh
# from a client, confirm the replica answers at all
dig @192.168.219.127 example.com

# confirm replication landed: a filter list added on the primary shows up here
curl -su "$REPLICA_USERNAME:$REPLICA_PASSWORD" \
  http://192.168.219.127:3080/control/filtering/status | head

# then the real test — stop the primary and confirm clients keep resolving
ssh marshall@192.168.219.100 'docker stop adguardhome'
ssh marshall@192.168.219.100 'docker start adguardhome'
```
