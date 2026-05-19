# cloudflared-sssup

Second cloudflared instance dedicated to the **sssup** Cloudflare account, which holds the `maji.you` and `irang.me` zones (and any future sssup-related zones). The original [`cloudflared/`](../cloudflared/) deployment serves the `marshallku.dev` account; tunnels are account-scoped so a separate pod with its own token is needed for any other account's zones, but one cloudflared instance can handle every zone on the same account.

Pinned to `mgmt01`. Public hostnames (ingress rules) are managed in the Cloudflare Zero Trust dashboard, not in this manifest — the deployment runs in token-based "remotely managed" mode.

## Layout

```
cloudflared-sssup/
├── namespace.yaml
├── deployment.yaml
├── secret.yaml.example       # template
└── sealed-secret.yaml        # generated via kubeseal (commit this; not committed yet)
```

## Bootstrap

### 1. Create the tunnel on the maji.you account

1. https://one.dash.cloudflare.com → switch to the **maji.you** account
2. **Networks** → **Tunnels** → **Create a tunnel**
3. Connector: `Cloudflared`
4. Tunnel name: `sssup` (or anything — display only)
5. Skip the connector install screen — we run it in k8s, not on a host
6. Copy the **tunnel token** from the install command (the long string after `--token`)

### 2. Seal the token

```sh
cp secret.yaml.example /tmp/cf-sssup-secret.yaml
# edit /tmp/cf-sssup-secret.yaml and replace `your-token-here` with the actual token

kubeseal \
  --controller-namespace kube-system \
  --controller-name sealed-secrets \
  --format yaml \
  < /tmp/cf-sssup-secret.yaml \
  > sealed-secret.yaml

shred -u /tmp/cf-sssup-secret.yaml
```

Commit `sealed-secret.yaml` only (the plain `secret.yaml` should never reach git).

### 3. Apply

```sh
kubectl apply -f namespace.yaml
kubectl apply -R -f .
```

Pod should come up `1/1 Running` in a few seconds. Verify the tunnel is registered:

```sh
kubectl -n cloudflared-sssup logs deploy/cloudflared --tail=20
# look for: "Registered tunnel connection" and "Connection ... registered"
```

### 4. Add public hostnames in the Cloudflare dashboard

Back in the tunnel's **Public Hostnames** tab, add:

| Subdomain | Domain | Type | URL |
| --- | --- | --- | --- |
| `dev` | `maji.you` | HTTP | `maji-frontend.maji-dev.svc.cluster.local:3000` |
| `dev-api` | `maji.you` | HTTP | `maji-api.maji-dev.svc.cluster.local:8080` |
| `api` | `irang.me` | HTTP | `irang-api.irang.svc.cluster.local:8080` |
| `admin` | `irang.me` | HTTP | `irang-admin-web.irang.svc.cluster.local:3000` |

CF auto-creates the orange-cloud DNS records. `c1.irang.me` is added separately as an R2 custom domain (R2 bucket page, not the tunnel).

### 5. Verify

```sh
curl -sS -o /dev/null -w "%{http_code}\n" https://dev-api.maji.you/api/health   # expect 200
curl -sS -o /dev/null -w "%{http_code}\n" https://dev.maji.you                  # expect 200
```

## Notes

- The deployment Kind=`Deployment` with `name: cloudflared`. The namespace (`cloudflared-sssup`) is what differentiates this from the marshallku.dev instance — same resource name is fine because the namespaces are different.
- Both cloudflared pods land on `mgmt01`. Memory limit is 128Mi each; combined footprint is negligible.
- If a new maji.you subdomain is added later, only the dashboard needs updating — no manifest change.
