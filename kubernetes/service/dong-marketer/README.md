# dong-marketer — namespace prerequisites

The workload is a factory app ([`../../apps/dong-marketer/values.yaml`](../../apps/dong-marketer/values.yaml));
image and source: the private `marshallku/dong-marketer` repo. Apply these once by hand, as for life-wiki:

```sh
kubectl create namespace dong-marketer
kubectl apply -f kubernetes/service/dong-marketer/
```

| File | Why |
| --- | --- |
| `pv-pvc.yaml` | `/var/lib/k3s-data/dong-marketer` on k3s01, `Retain`. Holds `app.db`, `backups/`, `images/` |
| `sealed-secret.yaml` | `dong-marketer-secret`: `PASSWORD_HASH`, `BRIDGE_TOKEN`, `BRIDGE_CERT_SHA256`, `IG_*` |
| `sealed-ghcr-secret.yaml` | `ghcr-secret`: the live dash secret resealed for this namespace (SealedSecrets are namespace-bound) |

## Data directory owner

The image runs as UID 10001 and hostPath ignores `fsGroup`, so chown the directory once before the first
sync. Without SSH to the node, a one-off root pod does it:

```sh
kubectl -n dong-marketer run chown-data --rm -i --restart=Never --image=alpine:3.22 --overrides='{
  "spec": {"nodeSelector": {"kubernetes.io/hostname": "k3s01"},
    "containers": [{"name": "chown-data", "image": "alpine:3.22",
      "command": ["sh", "-c", "chown 10001:10001 /d && ls -lnd /d"],
      "volumeMounts": [{"name": "d", "mountPath": "/d"}]}],
    "volumes": [{"name": "d", "hostPath": {"path": "/var/lib/k3s-data/dong-marketer", "type": "DirectoryOrCreate"}}]}}'
```

## Secrets

`~/dev/dong-marketer/deploy/seal-secrets.sh` writes `sealed-secret.yaml` without echoing any value. It
asks for her password, takes the bridge token and fingerprint from the Mac mini's llm-bridge, and takes
`IG_*` from the environment (empty until the Meta app exists). Rerun it with `IG_*` exported to add the
Instagram credentials, then commit and apply again.

## AI backend

`BRIDGE_URL=https://10.10.0.50:8941`: llm-bridge on the Mac mini (wired LAN), which spawns the operator's
claude/codex/agy subscription CLIs. Pod egress arrives there from the node IP `10.20.0.20`, which is on
the bridge's allow-list. Kagi is off (`KAGI_URL=off`).
