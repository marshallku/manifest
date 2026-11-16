# maar — namespace secrets

The workload is a factory app ([`../../apps/maar/values.yaml`](../../apps/maar/values.yaml)): MAAR's family
sync server (`ghcr.io/marshallku/maar-api`, source `80rian/sssup` → `apps/maar/server`). Both secrets below are sealed,
safe in this public repo, and applied by hand once — the factory ApplicationSet only watches `kubernetes/apps/*`.

```sh
kubectl create namespace maar
kubectl apply -f kubernetes/service/maar/
```

| Secret | Why |
| --- | --- |
| `maar-secret` | `DATABASE_URL` for the `maar` database on the shared Postgres (`sslmode=disable`) |
| `ghcr-secret` | `ghcr.io/marshallku/maar-api` is a private package; cloned from `dash` |

## maar-secret

Created by [`seal-db.sh`](./seal-db.sh): `provision-db.sh maar` pipes the URL straight into `kubeseal`, so the
password never reaches a terminal, a file or a process argument. Run it outside any agent session. To start over:
`scripts/provision-db.sh maar --drop`, then run `seal-db.sh` again and restart the deployment.

## ghcr-secret

Cloned from `dash` like the other factory namespaces (one GHCR credential in the homelab):

```sh
cert=$(mktemp)
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"
kubectl -n dash get secret ghcr-secret -o json \
  | jq '{apiVersion, kind, type, data, metadata: {name: "ghcr-secret", namespace: "maar"}}' \
  | kubeseal --cert "$cert" --format yaml > kubernetes/service/maar/sealed-ghcr-secret.yaml
```
