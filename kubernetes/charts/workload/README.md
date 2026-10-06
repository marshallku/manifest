# workload chart

The single parameterized chart behind the homelab **app factory**. One app is
one `values.yaml`; the chart renders everything that app needs on the k3s
cluster. It replaces the hand-copied `namespace/deployment/service` YAML that
`irang`, `maji`, and `playzy` each duplicate today.

## What it renders

| Resource | Per | Notes |
| --- | --- | --- |
| `Deployment` | surface | `imagePullSecrets: ghcr-secret`, probes, resources |
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

## Not public: a tier C surface

A surface with no `host` gets no Ingress and stays cluster-internal. Adding
`nodePort` makes it reachable on every node's address instead, which is how
`edge01` fronts the admin UIs that must not leave the tailnet.

```yaml
surfaces:
  web:
    image: ghcr.io/marshallku/dash:latest
    port: 8080
    nodePort: 30800        # no host: -> no tunnel, no public DNS
```

Pair it with an nginx server block on `edge01` under `*.in.marshallku.dev`,
which is a DNS-only wildcard at the tailnet address — so this costs a server
block and no DNS work. Setting `host` *and* `nodePort` publishes the service
through the tunnel as well; that is almost never what a tier C surface wants.

## Stateful surface: an existing PVC, Recreate, slow boot

For an app that keeps state on disk (e.g. SQLite), mount a PVC you created
yourself. The chart only *references* claims — it never renders a PVC — so an
ArgoCD prune or a deleted `apps/<app>/` directory can never take the data with
it. Put the PV/PVC under `kubernetes/service/<app>/` (hostPath under
`/var/lib/k3s-data/<app>`, `Retain`) and apply it by hand, like the secrets.

```yaml
surfaces:
  web:
    image: ghcr.io/marshallku/life-wiki:<sha>
    port: 80
    strategy: Recreate          # never two pods on one SQLite file during a rollout
    startupProbe:               # liveness waits until the first probe succeeds
      periodSeconds: 10
      failureThreshold: 60      # 10 min for install/migrations
    volumes:
      - name: data
        claimName: life-wiki-data
        mountPath: /data
```

`Recreate` only covers rollouts; a manually deleted pod is still replaced
while the old one terminates, so the app itself must guard against two
writers (life-wiki holds a `flock` on the volume).

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
- `volumes` mount existing PVCs (`name`, `claimName`, `mountPath` all required); the chart never creates storage.
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
