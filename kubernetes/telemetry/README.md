# telemetry — public ingest (e.marshallku.dev)

The collector runs outside the cluster, on analytics01 (`docker-compose/analytics01/telemetry/`).
This directory only gives it a public address through the `cloudflare-tunnel` Ingress class
(`kubernetes/cloudflare-tunnel/`).

| File | Purpose |
| --- | --- |
| `namespace.yaml` | the `telemetry` namespace |
| `ingest.yaml` | selectorless Service `collector` + EndpointSlice → `10.20.0.42:8080`, and Ingress `ingest` |

Only `POST/OPTIONS /v1/batch` is public. The tunnel rule is the regex `/v1/batch$`; everything else on
the host is a 404 at the tunnel, so `/metrics` and `/healthz` stay LAN only. Browsers send
`?k=<publishable key>`, servers `Authorization: Bearer <server key>`. Publishable keys are public by
nature, so the collector limits each one (`PUBLISHABLE_*` in its compose file; over it → `429`).

## Apply

Not ArgoCD-managed, like `kubernetes/monitoring/`:

```sh
kubectl apply -f kubernetes/telemetry/namespace.yaml
kubectl apply -f kubernetes/telemetry/ingest.yaml
```

The controller adds the tunnel route and a `CNAME e → <tunnel>.cfargotunnel.com` (plus its
`_ctic_managed.e` TXT). Check what it rendered:

```sh
kubectl -n cloudflare-tunnel logs deploy/controlled-cloudflared-connector | grep 'Updated to new configuration' | tail -1
```

If analytics01 ever changes address, edit the EndpointSlice; the Service name stays.

## Check

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://e.marshallku.dev/metrics          # 404
curl -s -X OPTIONS -D - -o /dev/null https://e.marshallku.dev/v1/batch | grep -i allow-origin
curl -s 'https://e.marshallku.dev/v1/batch?k=pk_nope' -d '[]'                        # 401 unknown_key
```

## Roll back

```sh
kubectl delete -f kubernetes/telemetry/ingest.yaml
```

Deleting the Ingress alone is not proof the route is gone. Confirm the controller removed both, and the
host falls back to the `*.marshallku.dev` wildcard (which answers 525, not the collector):

```sh
kubectl -n cloudflare-tunnel logs deploy/controlled-cloudflared-connector | grep 'Updated to new configuration' | tail -1 | grep -c e.marshallku.dev   # 0
curl -s -o /dev/null -w '%{http_code}\n' 'https://e.marshallku.dev/v1/batch?k=pk_nope' -d '[]'                                  # not 401
```

The other tunnel hosts share the connector and are not touched by this.
