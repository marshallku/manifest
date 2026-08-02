# workload chart

The single parameterized chart behind the homelab **app factory**. One app is
one `values.yaml`; the chart renders everything that app needs on the k3s
cluster. It replaces the hand-copied `namespace/deployment/service` YAML that
`irang`, `maji`, and `playzy` each duplicate today.

## What it renders

| Resource | Per | Notes |
| --- | --- | --- |
| `Deployment` | surface | `imagePullSecrets: ghcr-secret`, `nodeSelector dev01`, probes, resources |
| `Service` (ClusterIP) | surface | no more manual NodePort allocation |
| `Ingress` | surface **with `host`** | `ingressClassName: cloudflare-tunnel` → tunnel + DNS auto-wired |
| `InfisicalSecret` | app (opt-in) | syncs the Infisical project into `<app>-secret` |

## Minimal values

```yaml
app: cooking-timer
surfaces:
  web:
    image: ghcr.io/marshallku/cooking-timer-web:latest
    port: 3000
    host: cooking.marshallku.dev        # <- this line = public subdomain + DNS
```

## Full example (web + api + secret + db)

```yaml
app: maji
surfaces:
  web:
    image: ghcr.io/marshallku/maji-web:latest
    port: 3000
    host: maji.marshallku.dev
    env:
      - name: API_URL
        value: http://maji-api:8080
  api:
    image: ghcr.io/marshallku/maji-api:latest
    port: 8080
    host: api.maji.marshallku.dev
    healthPath: /api/health
    secretEnv: [DATABASE_URL, JWT_SECRET]
secret:
  infisical:
    enabled: true
    projectSlug: maji-prd
database:
  enabled: true          # DB provisioning helper creates db+role, writes DATABASE_URL to Infisical
```

## Contract

- `app` and every surface's `image` + `port` are **required** (chart fails fast otherwise).
- `namespace` defaults to `app`; the managed secret defaults to `<app>-secret`.
- A surface **without** `host` stays cluster-internal (worker/api-private).
- `secretEnv` keys are pulled from the managed secret via `secretKeyRef`.
- `database.tier: dedicated` is the reserved escape hatch (future CloudNativePG);
  `shared` (default) uses the db01 Postgres instance via the provisioning helper.

## Prerequisites in the app namespace

- `ghcr-secret` — GHCR image pull secret.
- `infisical-universal-auth` — Infisical universal-auth credentials (when `secret.infisical.enabled`).

Apps are wired to the cluster through the ArgoCD **ApplicationSet** (git
generator over `kubernetes/apps/*`), so adding `kubernetes/apps/<app>/values.yaml`
is all it takes — no manual `kubectl apply`.

## Validate locally

```sh
helm lint kubernetes/charts/workload -f kubernetes/apps/<app>/values.yaml
helm template <app> kubernetes/charts/workload -f kubernetes/apps/<app>/values.yaml
```
