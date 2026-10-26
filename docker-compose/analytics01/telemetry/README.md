# telemetry (analytics01)

The app factory's product events: ClickHouse 25.8 and the telemetry collector. Code, tracking plan
and tests: [marshallku/telemetry](https://github.com/marshallku/telemetry). Design:
[Factory Telemetry](https://claude.ai/artifact/SjcaJ6npgSYtQ6dC4VURHt).

| What | Where |
|---|---|
| Host | `analytics01` (pve02 VM 301, 10.20.0.42), infra `tofu/analytics01.tf` |
| Data | `/srv/analytics/{clickhouse,spool,demo}` on the `vault` HDD. **Disposable by decision**: not backed up |
| Collector | `10.20.0.42:8080` — `POST /v1/batch`, `/metrics` (Prometheus job `telemetry`), `/healthz` |
| ClickHouse | `10.20.0.42:8123`, users `default` (compose network only), `collector` (INSERT raw.events), `grafana_ro` (SELECT, readonly) |
| Dashboards | Grafana → *Telemetry · Factory Overview*, *Telemetry · App Product* (`kubernetes/monitoring/grafana/`) |

Nothing is public yet: there is no ingress. Real apps get an `e.marshallku.dev` route when they start sending.

## Deploy

```sh
cd ~/dev/manifest/docker-compose/analytics01/telemetry
git pull
docker compose up -d      # migrate runs to completion first, then the collector starts
```

Host-only files (gitignored): `.env` from `env.example` (three ClickHouse passwords, `chmod 600`) and
`keys.yaml` from `keys.example.yaml` (ingest keys; server keys are secrets). The Grafana side needs
`CH_GRAFANA_PASSWORD` as `clickhouse-password` in the `grafana-credentials` secret.

The image is private: `gh auth token | ssh marshall@10.20.0.42 docker login ghcr.io -u marshallku --password-stdin`.
Bumping it = change the sha in `docker-compose.yml`; `migrate` applies any new typed views.

## Demo data

Everything fake lives in `env = 'demo'`, sent through the real collector by `telemetry fake`:

```sh
# 30 days of history, plus the truth the dashboards' SQL must reproduce
docker compose run --rm fake fake --endpoint http://collector:8080 --backfill 30 --truth /demo/truth.json
set -a; . ./.env; set +a
docker compose run --rm -e CLICKHOUSE_URL=http://clickhouse:8123 -e CLICKHOUSE_USER=default \
    -e CLICKHOUSE_PASSWORD="$CH_ADMIN_PASSWORD" fake verify --truth /demo/truth.json

# a live trickle every 30 s (keeps freshness and pipeline panels moving)
docker compose --profile demo up -d fake
docker compose --profile demo stop fake

# wipe it
docker exec clickhouse clickhouse-client --password "$CH_ADMIN_PASSWORD" \
    -q "ALTER TABLE raw.events DELETE WHERE env = 'demo'"
```

`kubernetes/monitoring/grafana/check-telemetry-dashboards.py` runs every panel through Grafana's
query API for every app and fails on errors or unexpectedly empty panels.

## Rebuild from zero

The data disk is disposable, so recovery is a rebuild, not a restore:

1. Infra: `tofu apply` recreates VM 301 if needed; run `bootstrap/guest-common.sh`, `docker.sh`,
   `analytics01.sh` (formats an empty data disk, mounts `/srv/analytics`, makes the directories).
2. Clone manifest, put back `.env` and `keys.yaml` (new passwords are fine; update the Grafana
   secret to match), `docker login ghcr.io`.
3. `docker compose up -d` — `migrate` recreates every table and view.
4. Optional: the demo backfill above.

Events sent while the host is down are retried by SDKs for up to 24 hours (their queue lifetime).

## Capacity notes

- ClickHouse is held to half of the VM's 8 GiB (`clickhouse/config.d/small-server.xml`); system log
  tables keep 7 days and the profiling logs are off, because they write all day on a spinning disk.
- The collector answers `503` + `Retry-After` when 2,000,000 rows are pending or the spool disk has
  under 5% free. `telemetry_flush_blocked{reason}` = 1 means ClickHouse is refusing inserts; nothing
  is lost, the queue drains once the cause is fixed.
- `core.events` and `core.events_sessionized` compute dedup and sessions per query. That is fine at
  tens of thousands of events a day; past a few million rows, materialise them (design doc §05).
