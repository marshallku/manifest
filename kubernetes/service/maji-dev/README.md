# maji-dev

Development environment for [`maji`](../maji/), running parallel on the same cluster. Differs from prod only in:

- **Namespace** — `maji-dev` (vs `maji`)
- **Domain** — `dev.maji.you` / `dev-api.maji.you` (vs `maji.you` / `api.maji.you`)
- **NodePorts** — frontend `30502`, api `30503` (vs `30500` / `30501`)
- **DB** — `postgres-dev` on db01 port 5433 (separate container, separate volume)
- **R2 bucket** — `maji-dev-staging` served via `c1-dev.maji.you`
- **Secret backend** — Infisical (vs sealed-secret in prod)

Same node placement (`dev01`), same image registry. OAuth apps are **separate** (dev-only Kakao/Google clients) so dev tokens never authenticate against prod.

## Layout

```
maji-dev/
├── namespace.yaml
├── api/{deployment,service}.yaml
├── frontend/{deployment,service}.yaml
├── infisical-secret.yaml                    # InfisicalSecret CR (commit)
├── infisical-credentials.yaml.example       # template for universal-auth bootstrap
└── sealed-ghcr-secret.yaml.example          # template (run kubeseal to generate the real file)
```

Files committed to git: everything except `infisical-credentials.yaml` (plain) and the unsealed `*.yaml` from the templates.

## Bootstrap (one-time)

Prerequisites:

- Infisical instance running (`kubernetes/infisical/`).
- Infisical Operator installed (`kubernetes/infisical-operator/`).
- ArgoCD running with the `miniapp` AppProject (same as prod).

### 1. Create the dev R2 bucket

In Cloudflare R2 console:

- Bucket name: `maji-dev-staging`
- Public access: enable, custom domain `c1-dev.maji.you`
- Generate a new API token scoped to this bucket only — keep `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` for step 3.

### 2. Stand up postgres-dev on db01

On db01:

```sh
cd ~/manifest/docker-compose/postgres-dev
cat > .env <<EOF
POSTGRES_USER=maji
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
POSTGRES_DB=maji_dev
POSTGRES_PORT=5433
EOF
docker compose up -d
```

Save the password — it goes into the Infisical `DATABASE_URL` next. Connection string for the cluster:

```
postgres://maji:<password>@db01.local:5433/maji_dev?sslmode=disable
```

### 3. Set up the Infisical project

In the Infisical UI (https://infisical.marshallku.com):

1. Create project `maji` (slug: `maji`).
2. Add environment `dev` (slug: `dev`). Optionally add `prod` later for the prod migration.
3. In the `dev` env, populate these secret keys at path `/`:
   - `DATABASE_URL` — the connection string from step 2
   - `JWT_SECRET` — `openssl rand -base64 48`
   - `KAKAO_CLIENT_ID`, `KAKAO_CLIENT_SECRET` — from a separate Kakao dev app (redirect URI `https://dev.maji.you/oauth/callback`)
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — from a separate Google OAuth client (same redirect)
   - `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` — from step 1
4. Create a Machine Identity scoped to project `maji`, env `dev`, with **Universal Auth** enabled. Copy the client ID and client secret.

### 4. Apply the manifests

```sh
kubectl apply -f kubernetes/service/maji-dev/namespace.yaml
```

Bootstrap the universal-auth secret (plain `Secret`, not sealed — it lives only in this namespace and is the entry point for everything else):

```sh
cp kubernetes/service/maji-dev/infisical-credentials.yaml.example /tmp/infisical-creds.yaml
# edit /tmp/infisical-creds.yaml and replace the two REPLACE_* values
kubectl apply -f /tmp/infisical-creds.yaml
rm /tmp/infisical-creds.yaml
```

Generate the sealed ghcr-secret following the comment in `sealed-ghcr-secret.yaml.example`, commit the resulting `sealed-ghcr-secret.yaml`.

### 5. Wire up ArgoCD

Three new `Application`s in the `argocd` namespace (same `miniapp` project as prod):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: maji-api-dev
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/maji-dev/api
  destination:
    server: https://kubernetes.default.svc
    namespace: maji-dev
  syncPolicy:
    automated: { prune: true, selfHeal: true }
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: maji-web-dev
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/maji-dev/frontend
  destination:
    server: https://kubernetes.default.svc
    namespace: maji-dev
  syncPolicy:
    automated: { prune: true, selfHeal: true }
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: maji-secret-dev
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/maji-dev
    # Non-recursive (default) → only top-level *.yaml files are applied.
    # Skips api/ and frontend/ subdirs (those have their own Applications) and
    # the *.yaml.example templates (which ArgoCD ignores by extension).
  destination:
    server: https://kubernetes.default.svc
    namespace: maji-dev
  syncPolicy:
    automated: { prune: true, selfHeal: true }
```

The split mirrors prod (`maji-api`, `maji-web` are separate Applications). The third app picks up the namespace, InfisicalSecret CR, and sealed ghcr-secret from the top-level dir.

### 6. Cloudflared routes

Add two public hostname routes in the existing tunnel:

| Hostname | Service |
| --- | --- |
| `dev.maji.you` | `http://maji-frontend.maji-dev.svc.cluster.local:3000` |
| `dev-api.maji.you` | `http://maji-api.maji-dev.svc.cluster.local:8080` |

### 7. Verify

```sh
kubectl -n maji-dev get infisicalsecret maji-secret -o yaml | grep -A5 status:   # expect Synced
kubectl -n maji-dev get secret maji-secret -o yaml                                # expect populated keys
kubectl -n maji-dev rollout status deploy/maji-api --timeout=2m
kubectl -n maji-dev rollout status deploy/maji-frontend --timeout=2m
curl -s https://dev-api.maji.you/api/health
```

## Day 2

- Image tag updates land via CI commits to `kubernetes/service/maji-dev/{api,frontend}/deployment.yaml` (same flow as prod, see recent `deploy(maji): update image tag` commits).
- Secret rotation happens entirely in the Infisical UI — operator picks up changes within `resyncInterval` (60s) and patches the managed `maji-secret`.
- Schema migrations: run against `postgres-dev` directly, no impact on prod.
