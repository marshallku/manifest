# aide-push — namespace secrets

The workload is a factory app ([`../../apps/aide-push/values.yaml`](../../apps/aide-push/values.yaml));
these are the two secrets its namespace needs. Both are sealed and applied by hand once — the
factory ApplicationSet only watches `kubernetes/apps/*`.

```sh
kubectl create namespace aide-push
kubectl apply -f kubernetes/service/aide-push/
```

| Secret | Why |
| --- | --- |
| `aide-push-secret` | `AIDE_APNS_KEY` (the team's `.p8`, PEM text), `AIDE_APNS_KEY_ID`, `AIDE_APNS_TEAM_ID` |
| `ghcr-secret` | `ghcr.io/marshallku/aide-server` is a private package; cloned from `dash` |

## Resealing

The source of truth is AIDE's `deploy/secrets/apns.p8` and the ids in `deploy/.env`:

```sh
cert=$(mktemp)
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"
set -a; . ~/dev/aide/deploy/.env; set +a
kubectl create secret generic aide-push-secret -n aide-push \
  --from-file=AIDE_APNS_KEY="$HOME/dev/aide/deploy/secrets/apns.p8" \
  --from-literal=AIDE_APNS_KEY_ID="$AIDE_APNS_KEY_ID" \
  --from-literal=AIDE_APNS_TEAM_ID="$AIDE_APNS_TEAM_ID" \
  --dry-run=client -o yaml \
  | kubeseal --cert "$cert" --format yaml > kubernetes/service/aide-push/sealed-secret.yaml
kubectl apply -f kubernetes/service/aide-push/
kubectl -n aide-push rollout restart deployment/aide-push-api
```

## Image

`relay-<sha>` tags are built from AIDE master without cutting an AIDE release (amd64 only, the
node's arch); after an AIDE release the tag can move to `vX.Y.Z`.
