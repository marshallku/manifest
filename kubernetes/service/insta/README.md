# insta — namespace prerequisites

The workload is a factory app ([`../../apps/insta/values.yaml`](../../apps/insta/values.yaml));
image and source: the private `marshallku/insta` repo. Apply these once by hand, as for dong-marketer:

```sh
kubectl create namespace insta
kubectl apply -f kubernetes/service/insta/
```

| File | Why |
| --- | --- |
| `pv-pvc.yaml` | `/var/lib/k3s-data/insta` on k3s01, `Retain`. Holds `app.db`, `backups/`, `worker.lock` |
| `sealed-secret.yaml` | `insta-secret`: `OWNER_PASSWORD_HASH`, `TOKEN_KEY`, `IG_APP_SECRET`, `IG_VERIFY_TOKEN` |
| `sealed-ghcr-secret.yaml` | `ghcr-secret`: dong-marketer's live pull secret resealed for this namespace |

## Data directory owner

The image runs as UID 10001 and hostPath ignores `fsGroup`; chown `/var/lib/k3s-data/insta` once with the
one-off root pod from [`../dong-marketer/README.md`](../dong-marketer/README.md#data-directory-owner)
(namespace and path `insta`).

## Secrets

`~/dev/insta/deploy/seal-secrets.sh` writes `sealed-secret.yaml` without echoing any value. It keeps the
live `TOKEN_KEY` (rotating it makes every stored Instagram token unreadable, so every account must
reconnect) and copies `IG_APP_SECRET` / `IG_VERIFY_TOKEN` from `dong-marketer-secret` (same Meta app).
`REUSE_DONG_PASSWORD=1` copies dong-marketer's password hash as the owner's.
