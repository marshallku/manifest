# life-wiki — namespace prerequisites

The workload is a factory app ([`../../apps/life-wiki/values.yaml`](../../apps/life-wiki/values.yaml));
image and source: the private `marshallku/life-wiki` repo. These files are what its namespace needs
before that will sync. They are applied by hand once — the factory ApplicationSet only watches
`kubernetes/apps/*`.

```sh
kubectl create namespace life-wiki
kubectl apply -f kubernetes/service/life-wiki/
```

| File | Why |
| --- | --- |
| `pv-pvc.yaml` | `/var/lib/k3s-data/life-wiki` on k3s01, `Retain`. Holds `sqlite/`, `images/`, `backups/` |
| `sealed-secret.yaml` | `life-wiki-secret`: `MW_SECRET_KEY`, `MW_UPGRADE_KEY`, `MW_ADMIN_PASSWORD` |
| `sealed-ghcr-secret.yaml` | `ghcr-secret`, cloned from `dash` (one GHCR credential in the homelab) |

## Admin account

`MW_ADMIN_PASSWORD` is only used for the very first install (user `Admin`); changing it later
does nothing. Read it without echoing it into a shared terminal:

```sh
kubectl -n life-wiki get secret life-wiki-secret -o jsonpath='{.data.MW_ADMIN_PASSWORD}' | base64 -d | pbcopy
```

## Agent (wai) access

Create a bot password once; the generated value is printed by the script:

```sh
kubectl -n life-wiki exec deploy/life-wiki-web -- runuser -u www-data -- \
    php maintenance/run.php createBotPassword --appid wai \
    --grants basic,highvolume,editpage,createeditmovepage,uploadfile,uploadeditmovefile \
    Admin "$(LC_ALL=C tr -dc '0-9a-w' </dev/urandom | head -c 32)"
```

Bot passwords must match `[0-9a-w]{32}`; anything else is silently treated as a normal password.

## Upgrades, backups, rollback

On every new image SHA the container backs up `my_wiki.sqlite` to
`backups/pre-update-from-<previous sha>.sqlite` before running `update.php`.
To roll back: pin the previous image SHA in `values.yaml`, then with the pod scaled to 0
copy that backup over `sqlite/my_wiki.sqlite` and write the previous SHA to `/data/.applied-build`.

These backups sit on the same disk; they protect against a bad migration, not against
losing the node. Off-node backups are still a TODO.

## Resealing

```sh
cert=$(mktemp)
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"
kubectl create secret generic life-wiki-secret -n life-wiki \
  --from-literal=MW_SECRET_KEY=... --from-literal=MW_UPGRADE_KEY=... --from-literal=MW_ADMIN_PASSWORD=... \
  --dry-run=client -o yaml | kubeseal --cert "$cert" --format yaml > kubernetes/service/life-wiki/sealed-secret.yaml
```
