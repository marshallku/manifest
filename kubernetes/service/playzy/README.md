# playzy — namespace prerequisites

The workload is a factory app ([`../../apps/playzy/values.yaml`](../../apps/playzy/values.yaml)).
Apply once by hand, as for insta:

```sh
kubectl apply -f kubernetes/service/playzy/sealed-secret.yaml
```

| File | Why |
| --- | --- |
| `sealed-secret.yaml` | `playzy-secret`: `PLAYZY_DATABASE_URL` (role `playzy` on db01, which owns the `playzy` database), `PLAYZY_ADMIN_TOKEN` |

There is no `ghcr-secret`: both images are public.

## AI provider

Generating a story needs one of these; `/healthz` and `/v1/quota` work without either.

- `PLAYZY_AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`: the launch provider, no extra surface.
- A `kagi` surface + `KAGI_EMAIL` / `KAGI_PASSWORD`: dev-only, unofficial.

Add the keys by resealing `playzy-secret`, then list them under the api surface's `secretEnv`.
