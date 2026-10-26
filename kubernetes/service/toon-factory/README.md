# toon-factory — namespace prerequisites

The workload is a factory app ([`../../apps/toon-factory/values.yaml`](../../apps/toon-factory/values.yaml)); source and
image: the private `marshallku/toon-factory` repo (`deploy/` there has the scripts). Applied once by hand:

```sh
kubectl create namespace toon-factory
kubectl apply -f kubernetes/service/toon-factory/
```

| File | Why |
| --- | --- |
| `pv-pvc.yaml` | `/var/lib/k3s-data/toon-factory` on k3s01, `Retain`. Holds `app.db`, `backups/`, `assets/`, `exports/`, `previews/` |
| `sealed-secret.yaml` | `toon-factory-secret`: `PASSWORD_HASH`, `BRIDGE_TOKEN`, `BRIDGE_CERT_SHA256`, `MEDIA_BRIDGE_TOKEN` — `deploy/seal-secrets.sh` |
| `sealed-ghcr-secret.yaml` | `ghcr-secret`: the live dong-marketer pull secret resealed for this namespace |

The data directory is owned by UID 10001 (chowned once with the one-off pod in `../dong-marketer/README.md`).
