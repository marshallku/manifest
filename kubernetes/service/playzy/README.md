# playzy (prd)

Production deployment for **playzy** — the toddler bedtime-story app
([`playzy`](https://github.com/marshallku/playzy) repo). One surface:

- **playzy-api** — Go single binary (`backend/`), the AI gateway between the
  Flutter app and the AI provider (ADR 0001). Exposes the stable Playzy story
  contract (`POST /v1/stories`, `GET /v1/quota`, `POST /v1/credits`,
  `GET /v1/catalog/situations`, `GET /healthz`) and enforces the authoritative
  free-tier + credit quota (ADR 0002) in a crash-safe SQLite ledger.

The mobile app is the only client; there is no web frontend deployed here. Point
a build at the API with
`flutter build --dart-define=PLAYZY_API_BASE_URL=https://api-playzy.marshallku.dev`.

## AI backend (kagi-serve sidecar)

kagi is a **reverse-engineered, unofficial, dev/personal** Kagi Assistant client
(ADR 0001) — not shippable inside the app and single-user. It runs as a **native
sidecar** (initContainer with `restartPolicy: Always`, image
`ghcr.io/marshallku/kagi`) in the same pod as the api, hosting `/chat` on
`localhost:8921`. The api reaches it via `KAGI_SERVE_URL=http://127.0.0.1:8921`.
This mirrors the irang wiring. Swapping to a real provider (OpenAI/Anthropic) is
a server-side change confined to `callAI` in `backend/main.go` — the app never
changes.

## Differences from `maji/` and `irang/`

| Concern | maji/irang prd | playzy prd |
| --- | --- | --- |
| Namespace | `maji` / `irang` | `playzy` |
| Secret backend | SealedSecret / Infisical | **SealedSecret** (`sealed-secret.yaml`) |
| Cloudflare account | sssup (`maji.you`, `irang.me`) | **marshallku.dev** — served by the `cloudflared/` deployment, not `cloudflared-sssup/` |
| API domain | `api.maji.you` / `api.irang.me` | `api-playzy.marshallku.dev` |
| Web domain | root zone | `playzy.marshallku.dev` (wired separately by the owner; not served here) |
| Datastore | Postgres on db01 | **local SQLite** on a dev01 hostPath PVC (`pvc.yaml`) |
| AI backend | kagi-serve sidecar | **same** kagi-serve sidecar |
| NodePort | 30500/30501/30504/… | **30511** (api) |

## Layout

```
playzy/
├── namespace.yaml
├── pvc.yaml                        # hostPath PV + PVC for the SQLite ledger (dev01)
├── api/{deployment,service}.yaml   # api container + kagi-serve sidecar, NodePort 30511
├── secret.yaml.example             # template → seal to sealed-secret.yaml
├── sealed-ghcr-secret.yaml.example # template → seal to sealed-ghcr-secret.yaml
└── argocd-application.yaml.example  # register with ArgoCD (apply once, by hand)
```

Files committed to git: everything except the plaintext `secret.yaml`
(gitignored via `**/secret.yaml`). Commit the sealed outputs
(`sealed-secret.yaml`, `sealed-ghcr-secret.yaml`).

## Bootstrap

1. **Sidecar node storage.** Ensure the hostPath in `pvc.yaml`
   (`/mnt/hdd/data/dev01/playzy`) exists / is creatable on **dev01**. Adjust the
   path if dev01's data disk is mounted elsewhere.

2. **App secret.** Fill `KAGI_EMAIL` / `KAGI_PASSWORD` (kagi login) and
   optionally `PLAYZY_ADMIN_TOKEN`, then seal:

   ```sh
   cp secret.yaml.example /tmp/playzy-secret.yaml
   # edit /tmp/playzy-secret.yaml
   kubeseal --controller-namespace kube-system --controller-name sealed-secrets \
     --format yaml < /tmp/playzy-secret.yaml > sealed-secret.yaml
   rm /tmp/playzy-secret.yaml
   ```

3. **GHCR pull secret.** Follow `sealed-ghcr-secret.yaml.example` to generate
   `sealed-ghcr-secret.yaml` (namespace `playzy`).

4. **Register with ArgoCD** (once):

   ```sh
   kubectl apply -f kubernetes/service/playzy/argocd-application.yaml.example
   ```

5. **Public hostname.** In the Cloudflare Zero Trust dashboard (the
   **marshallku.dev** account, served by the existing `cloudflared/` tunnel), add
   a public hostname `api-playzy.marshallku.dev` → `http://<node-ip>:30511`.
   Ingress rules are managed in the dashboard (remotely-managed tunnel), not in
   this repo.

## CI/CD

The [`playzy`](https://github.com/marshallku/playzy) repo's
`deploy-backend.yml` builds `ghcr.io/marshallku/playzy-backend`, pushes `:prd`
and `:<sha>`, then bumps the image tag in `api/deployment.yaml` here and pushes —
ArgoCD syncs the new SHA. It needs a `MANIFEST_REPO_TOKEN` secret (write access
to this repo), the same pattern maji/irang use.
