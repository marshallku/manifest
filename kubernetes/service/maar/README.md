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
| `maar-secret` | `DATABASE_URL` for the `maar` database on the shared Postgres (`sslmode=disable`); `MAAR_ADMIN_TOKEN` for the content admin routes |
| `ghcr-secret` | `ghcr.io/marshallku/maar-api` is a private package; cloned from `dash` |

## maar-secret

Created by [`seal-db.sh`](./seal-db.sh): `provision-db.sh maar` pipes the URL straight into `kubeseal`, so the
password never reaches a terminal, a file or a process argument. Run it outside any agent session. It merges into
the existing `sealed-secret.yaml`, keeping `MAAR_ADMIN_TOKEN`. To start over:
`scripts/provision-db.sh maar --drop`, then run `seal-db.sh` again and restart the deployment.

## MAAR_ADMIN_TOKEN

The bearer token for maar-server's `/v1/admin/*` routes. The irang-dev admin console (`admin-dev.irang.me`, MAAR menu)
holds the same value as `irang-dev/maar-admin-token` (`../irang-dev/admin/sealed-maar-admin-token.yaml`) and attaches
it server side after checking the irang admin session. Both were sealed in one pass from a token that only ever lived
in shell memory: `openssl rand -hex 32`, then `kubectl create secret … --from-file=MAAR_ADMIN_TOKEN=<(printf %s
"$token") --dry-run=client -o yaml | kubeseal --cert … --format yaml`, with `--merge-into sealed-secret.yaml` for
`maar-secret`. To rotate, do the same for both files, `kubectl apply -f kubernetes/service/maar/`, and let ArgoCD sync
`irang-admin-web-dev`; then restart both deployments.

**Order:** the factory app reads `MAAR_ADMIN_TOKEN` from `maar-secret` (`secretEnv`), but ArgoCD does not apply this
directory. Apply the sealed secret and check the key unsealed (`kubectl -n maar get secret maar-secret -o json | jq -r
'.data | keys[]'` prints key names only, never values) *before* pushing a `values.yaml` that references a new key; otherwise the new
pod cannot start (the old one keeps serving).

## ANTHROPIC_API_KEY

"AI로 읽기" on maar-server calls Claude with this key; without it the route answers `ai_unconfigured`. Run
[`seal-anthropic.sh`](./seal-anthropic.sh) yourself (hidden prompt → kubeseal, merged into `sealed-secret.yaml` and
applied), check the key name is listed, *then* add `ANTHROPIC_API_KEY` to `secretEnv` in `../../apps/maar/values.yaml`
and commit both (the same secret-first order as `MAAR_ADMIN_TOKEN`).

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
