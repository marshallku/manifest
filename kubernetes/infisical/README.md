# Infisical

Self-hosted secret manager. Pinned to `mgmt01` alongside ArgoCD.

## Layout

```
infisical/
├── namespace.yaml
├── configmaps/configmap-infisical.yaml     # SITE_URL, REDIS_URL, NODE_ENV …
├── volumes/
│   ├── pv-pvc-postgres.yaml                # /mnt/hdd/data/mgmt01/infisical/postgres
│   └── pv-pvc-redis.yaml                   # /mnt/hdd/data/mgmt01/infisical/redis
├── statefulsets/statefulset-postgres.yaml  # postgres:16-alpine
├── deployments/
│   ├── deployment-redis.yaml               # redis:7-alpine
│   └── deployment-infisical.yaml           # infisical/infisical:v0.159.25
├── services/
│   ├── service-postgres.yaml               # headless, 5432
│   ├── service-redis.yaml                  # ClusterIP, 6379
│   └── service-infisical.yaml              # ClusterIP, 8080
└── secrets/
    ├── secret.yaml.example                 # template (do not commit real values)
    └── sealed-secret.yaml                  # generated via kubeseal (commit this)
```

## Bootstrap

### 1. Update `SITE_URL`

Edit `configmaps/configmap-infisical.yaml` — set `SITE_URL` to the public URL you will route through cloudflared (e.g. `https://infisical.marshallku.com`). It must be absolute and include the protocol; OAuth/email links will use it.

### 2. Generate secrets and seal

```sh
cp secrets/secret.yaml.example /tmp/infisical-secret.yaml

ENCRYPTION_KEY=$(openssl rand -hex 16)
AUTH_SECRET=$(openssl rand -base64 32)
PG_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')

sed -i "s|REPLACE_16_BYTE_HEX|${ENCRYPTION_KEY}|g" /tmp/infisical-secret.yaml
sed -i "s|REPLACE_32_BYTE_BASE64|${AUTH_SECRET}|g" /tmp/infisical-secret.yaml
sed -i "s|REPLACE_PG_PASSWORD|${PG_PASSWORD}|g" /tmp/infisical-secret.yaml

kubeseal \
  --controller-namespace kube-system \
  --controller-name sealed-secrets \
  --format yaml \
  < /tmp/infisical-secret.yaml \
  > secrets/sealed-secret.yaml

rm /tmp/infisical-secret.yaml
```

Commit `secrets/sealed-secret.yaml` only — never the plain `secret.yaml`.

### 3. Apply

Either via ArgoCD (recommended) or directly:

```sh
kubectl apply -f kubernetes/infisical/namespace.yaml
kubectl apply -R -f kubernetes/infisical/
```

ArgoCD `Application`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: infisical
  namespace: argocd
spec:
  project: default
  source:
    repoURL: <this repo>
    targetRevision: master
    path: kubernetes/infisical
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: infisical
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```

### 4. First-time admin signup

The backend runs DB migrations on startup. Once `kubectl -n infisical get pod` shows `infisical-*` ready, port-forward and create the first user:

```sh
kubectl -n infisical port-forward svc/infisical 8080:8080
# open http://localhost:8080 → "Sign Up" creates the admin account
```

After the first signup, signup is locked to invitations.

### 5. Expose via cloudflared

Add a public hostname route in the existing Cloudflare tunnel pointing to `http://infisical.infisical.svc.cluster.local:8080`. Keep `HTTPS_ENABLED=false` in the configmap — TLS terminates at Cloudflare.

## Node placement

All workloads land on `mgmt01`:

- `nodeSelector: kubernetes.io/hostname: mgmt01` on every pod
- `hostPath` PVs for postgres + redis bound to mgmt01 via `nodeAffinity`
- Same node as ArgoCD, cloudflared, and the monitoring stack (the sealed-secrets controller in `kube-system` has no node pin and may schedule elsewhere)

The hostPath dirs (`/mnt/hdd/data/mgmt01/infisical/postgres`, `…/redis`) are auto-created by the `DirectoryOrCreate` PV. The postgres/redis init containers chown them to uid 999.

## Migrating sealed-secrets → Infisical

1. Stand up Infisical (this directory) + install the [Infisical Operator](https://infisical.com/docs/integrations/platforms/kubernetes) into the cluster.
2. For each existing `SealedSecret` (e.g. `service/maji/sealed-secret.yaml`), copy the cleartext values into a new project in Infisical.
3. Replace the `SealedSecret` manifest with an `InfisicalSecret` CRD pointing at the same target Secret name.
4. Verify the workload still picks up env vars correctly, then delete the `SealedSecret`.
5. Once all consumers migrated, the sealed-secrets controller (`kubernetes/sealed-secrets/`) can be removed.

## CI integration (GitHub Actions)

After the instance is reachable publicly, configure a Machine Identity with OIDC trust for the GitHub repo, then in workflows:

```yaml
- uses: Infisical/secrets-action@v1
  with:
    domain: https://infisical.marshallku.com
    method: oidc
    identity-id: <machine-identity-id>
    project-slug: <slug>
    env-slug: prod
```

This removes long-lived `INFISICAL_TOKEN` from GitHub Secrets.
