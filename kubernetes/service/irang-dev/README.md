# irang-dev

Development environment for [`irang`](../irang/). Differs from prd only in:

- **Namespace** — `irang-dev`
- **Domain** — `api.dev.irang.me` / `admin.dev.irang.me`
- **NodePorts** — `30506` (api), `30507` (admin-web)
- **DB** — `postgres-dev` on db01 port `5443`, database `sssup_dev` (same instance as `maji-dev` / postgres-dev shares the cluster)
- **R2 bucket** — `irang-dev` (separate from `irang-prod`), served via `c1.dev.irang.me`
- **Image tag** — `:dev-placeholder` → CI rewrites to `:<sha>` on push to `develop` branch (see `.github/workflows/deploy-irang-dev.yml` in the sssup repo)
- **Infisical project** — `irang-dev` (env slug `dev`), separate from `irang-prd`
- **OAuth / wrapper / tokens** — none (admin-only, no user-facing OAuth)

Same node placement (`dev01`), same image registry, same cloudflared (`cloudflared-sssup` — shared tunnel handles both `irang.me` and `dev.irang.me` subdomains since `irang.me` is on the sssup Cloudflare account).

## Bootstrap

Identical to [`../irang/README.md`](../irang/README.md) except:

### 1. Cloudflare hostnames

Add in the `sssup` tunnel's **Public Hostnames** tab:

| Subdomain | Domain | Type | URL |
| --- | --- | --- | --- |
| `api.dev` | `irang.me` | HTTP | `irang-api.irang-dev.svc.cluster.local:8080` |
| `admin.dev` | `irang.me` | HTTP | `irang-admin-web.irang-dev.svc.cluster.local:3000` |
| `c1.dev` | `irang.me` | (origin) | R2 custom domain (set on the bucket page) |

### 2. R2 bucket

Create `irang-dev` bucket (separate from `irang-prod`) + custom domain `c1.dev.irang.me`. R2 token can be scoped to the dev bucket only.

### 3. DB (already done)

`sssup_dev` database exists on `postgres-dev` (port 5443). `maji_dev` was renamed to `sssup_dev` so maji + irang share the same postgres-dev DB with schema separation (`public` for maji, `irang` for irang).

DATABASE_URL for Infisical:

```
postgres://maji:<password>@192.168.219.130:5443/sssup_dev?sslmode=disable
```

(same password as the existing maji-dev DATABASE_URL — only the database name changed from `maji_dev`)

### 4. Infisical `irang-dev` project

- Project `irang-dev` (slug `irang-dev`)
- Env slug `dev`
- Keys at path `/`:
  - `DATABASE_URL` (above)
  - `JWT_SECRET` — generate fresh OR reuse maji-dev's (matters for the JWT paste flow into the admin)
  - `ADMIN_EMAILS` — comma-separated
  - `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` — `irang-dev` R2 token, or `placeholder` for now
- Identity `irang-dev-operator` with Universal Auth → clientId/clientSecret
- Project Access Control → add operator with `Viewer` role

### 5. ArgoCD Applications

Three apps named `irang-api-dev`, `irang-admin-web-dev`, `irang-secret-dev` — point at `kubernetes/service/irang-dev/{api,admin,}` paths. See parent README §6 for the YAML pattern.

### 6. CI

`develop` branch triggers `.github/workflows/deploy-irang-dev.yml` in the sssup repo. Images push to GHCR (`ghcr.io/80rian/irang-{api,admin-web}:dev` + `:<sha>`) and manifest commit-back rewrites the `:dev-placeholder` tag.

### 7. Verify

```sh
kubectl -n irang-dev rollout status deploy/irang-api
kubectl -n irang-dev rollout status deploy/irang-admin-web
curl -sS https://api.dev.irang.me/api/health
```

`https://admin.dev.irang.me` should render the admin UI; paste a `maji-dev` JWT to authenticate.
