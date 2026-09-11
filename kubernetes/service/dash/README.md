# dash — namespace secrets

The workload itself is a factory app ([`../../apps/dash/values.yaml`](../../apps/dash/values.yaml));
these are the two secrets its namespace needs before that will sync. Both are
sealed, so they are safe in this public repo, and both are applied by hand once
— the factory ApplicationSet only watches `kubernetes/apps/*`.

```sh
kubectl create namespace dash
kubectl apply -f kubernetes/service/dash/
```

| Secret | Why |
| --- | --- |
| `dash-secret` | `DASH_CONFIG_YAML` plus the credentials it references |
| `ghcr-secret` | `ghcr.io/marshallku/dash` is a private package |

## dash-secret

`DASH_CONFIG_YAML` is the **entire** `dash.yaml` as one key. dash reads its
config from that variable in preference to a file path, so there is no volume to
mount and the chart needed no change for it.

This is where the deployment's private half lives. The
[dash repo](https://github.com/marshallku/dash) is deliberately free of it — the
binary declares no hosts, ports or services — so this sealed blob and the
author's working copy are the only places the homelab's inventory exists.

`STATUS_TOKEN` and `INFLUX_TOKEN` are the `${...}` references inside that
config. dash refuses to start if one is missing rather than sending a blank
credential and collecting a 401.

## Resealing

Editing the config means resealing the whole secret — there is no partial
update. Working copy: `~/dev/dash/dash.yaml` (gitignored there).

```sh
cert=$(mktemp)
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"

kubectl create secret generic dash-secret -n dash \
  --from-file=DASH_CONFIG_YAML="$HOME/dev/dash/dash.yaml" \
  --from-literal=STATUS_TOKEN=... \
  --from-literal=INFLUX_TOKEN=... \
  --dry-run=client -o yaml \
  | kubeseal --cert "$cert" --format yaml > kubernetes/service/dash/sealed-secret.yaml
```

Then commit, `kubectl apply -f`, and restart the deployment — the config is read
once at startup:

```sh
kubectl -n dash rollout restart deployment/dash-web
```

## ghcr-secret

Cloned from an existing namespace rather than minting a new token, so there is
one GHCR credential in the homelab and not four. If it is ever rotated, every
namespace holding a copy has to be resealed.
