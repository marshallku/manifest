# irang (prd)

Production deployment for **irang** — the 영유아 큐레이션 + 가격비교 슈퍼앱
([`sssup`](https://github.com/80rian/sssup) repo). Phase 1 ships two surfaces:

- **irang-api** — Go / chi / pgx, single binary (`apps/irang/api`). Exposes
  `/api/admin/*` behind a maji-issued JWT + `ADMIN_EMAILS` allowlist (interim
  auth, replaced by `services/auth` + RS256 once that ships).
- **irang-admin-web** — Next.js 16 admin console (`apps/admin/web`).
  Editors paste a maji JWT into the topbar and the API gates from there.

No user-facing frontend yet — Phase 1 is editorial tooling only.

## Differences from `maji/`

| Concern | maji prd | irang prd |
| --- | --- | --- |
| Namespace | `maji` | `irang` |
| Secret backend | SealedSecret (`sealed-secret.yaml`) | **Infisical** (vault from day 1) |
| User domain | `maji.you` | `irang.me` (same Cloudflare account as maji.you) |
| API domain | `api.maji.you` | `api.irang.me` |
| Admin domain | — | `admin.irang.me` |
| R2 bucket | `maji-prod` | `irang-prod` |
| R2 public | `c1.maji.you` | `c1.irang.me` |
| NodePorts | 30500 frontend, 30501 api | 30504 api, 30505 admin |
| Postgres | db01 (`maji` database) | **same** db01 + `maji` database + `irang` schema (ADR-0012) |
| JWT_SECRET | own | **shared with maji prd** so admin can paste a maji JWT |
| Cloudflared | `cloudflared-sssup` (shared, same account) | **reuses** `cloudflared-sssup` — just add hostnames in the dashboard |

The dev environment was deliberately skipped — prd-first.

## Layout

```
irang/
├── namespace.yaml
├── api/{deployment,service}.yaml
├── admin/{deployment,service}.yaml
├── infisical-secret.yaml                    # InfisicalSecret CR (commit)
├── infisical-credentials.yaml.example       # template for universal-auth bootstrap
└── sealed-ghcr-secret.yaml.example          # template (run kubeseal to generate the real file)
```

Files committed to git: everything except `infisical-credentials.yaml` (plain
universal-auth, applied manually once) and any unsealed `*.yaml` derived from
the `.example` templates.

## Bootstrap (one-time)

Prerequisites:

- Infisical instance running ([`kubernetes/infisical/`](../../infisical/)).
- Infisical Operator installed ([`kubernetes/infisical-operator/`](../../infisical-operator/)).
- ArgoCD running with the `miniapp` AppProject (same as maji).
- SealedSecrets controller (`kube-system/sealed-secrets`) — used only for the
  ghcr image pull secret.
- [`cloudflared-sssup/`](../../cloudflared-sssup/) running (already serves
  `maji.you`; `irang.me` shares the same tunnel because both zones are on
  the same Cloudflare account).

### 1. Cloudflare — route `irang.me` through the existing tunnel

`irang.me` is on the **same Cloudflare account** as `maji.you`, so the
existing [`cloudflared-sssup/`](../../cloudflared-sssup/) deployment serves
both zones (cloudflared tokens are account-scoped — one instance per
account). No new pod, no new sealed-secret.

In the Cloudflare Zero Trust dashboard for that account, open the `sssup`
tunnel and add to **Public Hostnames**:

| Subdomain | Domain | Type | URL |
| --- | --- | --- | --- |
| `api` | `irang.me` | HTTP | `irang-api.irang.svc.cluster.local:8080` |
| `admin` | `irang.me` | HTTP | `irang-admin-web.irang.svc.cluster.local:3000` |
| `c1` | `irang.me` | (origin) | R2 custom domain — set in the R2 bucket page, not the tunnel |

The root `irang.me` is intentionally left unmapped — there's no user-facing
frontend yet. Point at a holding page (Cloudflare Pages) when needed.

If the `irang.me` zone hasn't been added to the account yet, do that first:
**Account → Add a site → `irang.me`**. Cloudflare auto-creates the orange-
cloud DNS records when the public hostnames above are saved.

### 2. R2 bucket — `irang-prod`

In Cloudflare R2 (the new `irang.me` account):

- Create bucket `irang-prod`.
- Public access: enable, custom domain `c1.irang.me`.
- Generate an API token scoped to this bucket only — keep
  `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` for step 4.

Phase 1 doesn't write to R2 yet (image uploads land in a later track), but
the bucket + tokens exist so the first upload PR doesn't have to touch infra.

### 3. Postgres — reuse maji's

irang shares maji prd's Postgres instance (ADR-0012, single DB + schema
separation). No new container, no new database — just the existing one.

The first irang-api boot:

1. Calls `renameLegacyShopSchema(ctx, pool)` — noop on prd because no `shop`
   schema exists.
2. Runs `CREATE SCHEMA IF NOT EXISTS irang`.
3. Applies migrations `0001_create.sql` … `0004_article_status_check.sql`
   inside the `irang` schema.

No manual DB work needed. The `DATABASE_URL` to put in Infisical is the same
string maji prd's secret holds (decrypt the existing sealed-secret on a
trusted host to read it once).

### 4. Infisical — create the `irang-prd` project

In the Infisical UI (http://infisical.marshallku.com:30200, LAN only):

1. **Create project** named `irang-prd`. Rename the auto-generated slug to
   plain `irang-prd` so it matches `infisical-secret.yaml`'s `projectSlug`.
   (CR returns 404 on any mismatch.)
2. Add environment with slug **`prd`**.
3. In `prd` populate these keys at path `/`:
   - `DATABASE_URL` — same connection string as maji prd's secret.
   - `JWT_SECRET` — same value as maji prd's `JWT_SECRET`. Required so
     admin editors can paste a maji-issued JWT into the irang admin and
     have signature verification pass against the same HMAC key.
   - `ADMIN_EMAILS` — comma-separated allowlist of editor emails
     (e.g. `marshall@kakao.com,editor@studio.com`).
   - `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` — R2 token from step 2.
4. Org → Access Control → Identities → create `irang-prd-operator` with
   **Universal Auth** enabled. Copy the client ID and client secret (shown
   once).
5. Project `irang-prd` → Access Control → Identities → add
   `irang-prd-operator` with role **Viewer**. Without this the operator
   gets a 404 ("project not found") even with valid credentials.

### 5. Apply the manifests

```sh
kubectl apply -f kubernetes/service/irang/namespace.yaml
```

Bootstrap the universal-auth secret (plain `Secret`, applied once outside
git — it's the entry point for everything else):

```sh
cp kubernetes/service/irang/infisical-credentials.yaml.example /tmp/irang-creds.yaml
# edit /tmp/irang-creds.yaml — paste the two values from step 4
kubectl apply -f /tmp/irang-creds.yaml
shred -u /tmp/irang-creds.yaml
```

Generate the sealed ghcr-secret following the comment in
`sealed-ghcr-secret.yaml.example`, commit the resulting
`sealed-ghcr-secret.yaml`.

### 6. Wire up ArgoCD

Three new `Application`s in the `argocd` namespace (same `miniapp` project
as maji):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: irang-api
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/irang/api
  destination:
    server: https://kubernetes.default.svc
    namespace: irang
  syncPolicy:
    automated: { prune: true, selfHeal: true }
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: irang-admin-web
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/irang/admin
  destination:
    server: https://kubernetes.default.svc
    namespace: irang
  syncPolicy:
    automated: { prune: true, selfHeal: true }
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: irang-secret
  namespace: argocd
spec:
  project: miniapp
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/service/irang
    # Non-recursive (default) → only top-level *.yaml files are applied.
    # Skips api/ and admin/ subdirs (those have their own Applications) and
    # the *.yaml.example templates (which ArgoCD ignores by extension).
  destination:
    server: https://kubernetes.default.svc
    namespace: irang
  syncPolicy:
    automated: { prune: true, selfHeal: true }
```

The split mirrors maji (`maji-api`, `maji-frontend` are separate
Applications). The third Application picks up the namespace, InfisicalSecret
CR, and sealed ghcr-secret from the top-level dir.

### 7. Verify

```sh
kubectl -n irang get infisicalsecret irang-secret -o yaml | grep -A5 status:   # expect Synced
kubectl -n irang get secret irang-secret -o yaml                                # expect populated keys
kubectl -n irang rollout status deploy/irang-api --timeout=2m
kubectl -n irang rollout status deploy/irang-admin-web --timeout=2m
curl -s https://api.irang.me/api/health
# Open https://admin.irang.me in a browser, paste a maji-prd JWT in the
# topbar — the admin should resolve /api/admin/whoami with admin=true.
```

## Day 2

- **Image tag updates** land via CI commits to
  `kubernetes/service/irang/{api,admin}/deployment.yaml` once
  `ci-irang-api.yml` and `ci-irang-admin-web.yml` are added to the sssup repo
  (mirroring `ci-maji-api.yml` / `ci-maji-web.yml`). Both images currently
  point at `:placeholder` — first CI push will rewrite the tag.
- **Secret rotation** happens in the Infisical UI — the operator picks up
  changes within `resyncInterval` (60s) and patches the managed `irang-secret`.
  No pod restart needed for Go env vars that come from the secret since
  `valueFrom.secretKeyRef` is read once at process start — bounce the
  deployment after rotation:
  `kubectl -n irang rollout restart deploy/irang-api`.
- **Schema migrations** run automatically on api boot against the `irang`
  schema. New SQL files go in `apps/irang/api/migrations/`, lex order
  applied. The tracker is `irang.schema_migrations`.
- **JWT rotation** must happen in lockstep with maji prd's `JWT_SECRET` since
  the admin reuses maji-issued tokens. Update both Infisical projects, then
  bounce both deployments. Replaced by RS256 once `services/auth` ships.

## Lifecycle invariants the code enforces

The api boots with these guarantees baked in:

- Every article carries a `disclosure` block at all times. `Create` / `PATCH`
  / `Publish` all run `injectDisclosure` (표시광고법 대응,
  [50-curation §2](https://github.com/80rian/sssup/blob/main/docs/50-curation.md#2-작성기--블록-모델)).
- `status`, `published_at`, `scheduled_at` cannot be set via `POST/PATCH` —
  only `POST /articles/{id}/publish` and `DELETE /articles/{id}` move them.
- `missing_kc=1` filter is scoped to the documented KC-required category
  prefixes (`baby/feeding`, `baby/diapering`, `baby/travel`, `baby/sleep`,
  `baby/toys`, `baby/bath`).
- All mutating admin actions write to `ops.audit_logs`.

Operators don't need to enforce these manually — they hold under any client.

## Open follow-ups (not in this PR)

- `.github/workflows/ci-irang-api.yml` + `.github/workflows/ci-irang-admin-web.yml`
  in the sssup repo — build + push to GHCR + commit the new tag back here.
- `services/auth` extraction and the RS256 cutover — removes the JWT_SECRET
  sharing between maji and irang.
- User-facing `irang.me` frontend (Phase 2+).
- IP allowlist on `admin.irang.me` via Cloudflare Access (currently only
  email-allowlist on the API; the admin UI itself is unauthenticated until
  the editor pastes a token).
