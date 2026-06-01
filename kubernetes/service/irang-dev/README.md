# irang-dev

Development environment for [`irang`](../irang/). Differs from prd only in:

- **Namespace** — `irang-dev`
- **Domain** — `api-dev.irang.me` (api) / `admin-dev.irang.me` (admin) / `dev.irang.me` (public web)
- **NodePorts** — `30506` (api), `30507` (admin-web), `30509` (web)
- **DB** — `postgres-dev` on db01 port `5443`, database `sssup_dev` (same instance as `maji-dev` / postgres-dev shares the cluster)
- **R2 bucket** — `irang-dev` (separate from `irang-prod`), served via `c1-dev.irang.me`
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
| `api-dev` | `irang.me` | HTTP | `irang-api.irang-dev.svc.cluster.local:8080` |
| `admin-dev` | `irang.me` | HTTP | `irang-admin-web.irang-dev.svc.cluster.local:3000` |
| `dev` | `irang.me` | HTTP | `irang-web.irang-dev.svc.cluster.local:3000` |
| `c1-dev` | `irang.me` | (origin) | R2 custom domain (set on the bucket page) |

### 2. R2 bucket

Create `irang-dev` bucket (separate from `irang-prod`) + custom domain `c1-dev.irang.me`. R2 token can be scoped to the dev bucket only.

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
  - `JWT_SECRET` — fresh random; only used to sign irang admin sessions (cookie `irang_admin_session`)
  - `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` — `irang-dev` R2 token, or `placeholder` for now
- Identity `irang-dev-operator` with Universal Auth → clientId/clientSecret
- Project Access Control → add operator with `Viewer` role

Admin accounts live in the DB (`irang.admin_users`), not in Infisical. Seed the first one with `admin-seed` (see §8).

### 5. ArgoCD Applications

Four apps named `irang-api-dev`, `irang-admin-web-dev`, `irang-web-dev`, `irang-secret-dev` — point at `kubernetes/service/irang-dev/{api,admin,web,}` paths. The apps are per-component (one path each), NOT a single recursive app, so a new component needs its own app — adding files under `web/` alone won't sync. See parent README §6 for the YAML pattern (`irang-web-dev` mirrors `irang-admin-web-dev`, only the `path` changes to `kubernetes/service/irang-dev/web`).

### 6. CI

`develop` branch triggers `.github/workflows/deploy-irang-dev.yml` in the sssup repo. Images push to GHCR (`ghcr.io/80rian/irang-{api,admin-web,web}:dev` + `:<sha>`) and manifest commit-back rewrites the `:dev-placeholder` tag. `irang-web` is the public reading surface (`apps/irang/web`) served at `dev.irang.me`.

### 7. Verify

```sh
kubectl -n irang-dev rollout status deploy/irang-api
kubectl -n irang-dev rollout status deploy/irang-admin-web
curl -sS https://api-dev.irang.me/api/health
```

`https://admin-dev.irang.me` should redirect to `/login`. Login requires a row in `irang.admin_users` — seed it first (§8).

### 8. Seed the first admin

The admin auth flow is invite-gated, so the first row has to be inserted directly. The image ships an `/irang-admin-seed` binary alongside the server:

```sh
kubectl -n irang-dev exec -it deploy/irang-api -- \
  /irang-admin-seed --email me@example.com --role admin
# Password is prompted twice on the TTY (no echo).
```

After that, sign in at `https://admin-dev.irang.me/login`. From there, generate invites for other admins/editors via the **사용자 → 초대 관리** page. Each invite is single-use, email-locked, and expires within 30 days.
