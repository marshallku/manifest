# cloudflare-tunnel — git-driven domain layer

The [STRRL cloudflare-tunnel-ingress-controller](https://github.com/STRRL/cloudflare-tunnel-ingress-controller)
turns a Kubernetes `Ingress` into a public Cloudflare Tunnel route. It watches
Ingresses with `ingressClassName: cloudflare-tunnel` and, through the Cloudflare
API, provisions the tunnel route + DNS CNAME and runs the cloudflared connector.

**A new public subdomain is one Ingress `host` line** — no Zero Trust dashboard
clicks, no NodePort. The `workload` chart emits that Ingress automatically for
any surface with a `host`.

## Contents

| File | Purpose |
| --- | --- |
| `namespace.yaml` | the `cloudflare-tunnel` namespace |
| `secret.yaml.example` | template for the Cloudflare credentials (account_id / tunnel_name / api_token) |
| `sealed-secret.yaml` | the sealed credentials, safe to commit (decrypted in-cluster by sealed-secrets) |
| `argocd-application.yaml` | ArgoCD app that installs the controller Helm chart (`helm.strrl.dev`, pinned `0.0.24`) |

## Cloudflare API token

Create a **custom API token** with:

- `Account · Cloudflare Tunnel · Edit`
- `Zone · DNS · Edit` (marshallku.dev)
- `Zone · Zone · Read` (marshallku.dev)

## Bootstrap (once)

```sh
# 1. seal the credentials (plaintext never leaves /tmp)
cp secret.yaml.example /tmp/cf.yaml         # fill account_id / api_token
kubeseal --controller-namespace kube-system --controller-name sealed-secrets \
  --format yaml < /tmp/cf.yaml > sealed-secret.yaml
rm /tmp/cf.yaml

# 2. apply (namespace + secret before the app)
kubectl apply -f namespace.yaml
kubectl apply -f sealed-secret.yaml
kubectl apply -f argocd-application.yaml
```

The controller adopts/creates a tunnel named `homelab-factory` and manages one
cloudflared connector for all `cloudflare-tunnel` Ingresses.

## Relationship to the old cloudflared tunnels

`kubernetes/cloudflared/` and `kubernetes/cloudflared-sssup/` are the legacy
**remotely-managed** tunnels — their routes live in the Zero Trust dashboard.
They keep serving the current NodePort apps (irang/maji/playzy) untouched. As
those apps migrate to Ingress (Phase 1), their hostnames move here and the old
tunnels are retired.
