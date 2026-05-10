# Secrets

`secret.yaml.example` is the template. Never commit a populated `secret.yaml`.

Workflow:

```sh
# 1. Copy and fill
cp secret.yaml.example /tmp/infisical-secret.yaml
# edit /tmp/infisical-secret.yaml — see ../README.md for openssl commands

# 2. Seal
kubeseal \
  --controller-namespace kube-system \
  --controller-name sealed-secrets \
  --format yaml \
  < /tmp/infisical-secret.yaml \
  > sealed-secret.yaml

# 3. Wipe plaintext
rm /tmp/infisical-secret.yaml
```

Only `sealed-secret.yaml` is safe to commit.

## Keys

| Key                  | Source                    | Notes                                                     |
| -------------------- | ------------------------- | --------------------------------------------------------- |
| `ENCRYPTION_KEY`     | `openssl rand -hex 16`    | 16-byte hex (32 chars). Encrypts secret values at rest.   |
| `AUTH_SECRET`        | `openssl rand -base64 32` | 32-byte base64. Signs auth tokens.                        |
| `POSTGRES_PASSWORD`  | `openssl rand -base64 24` | Used by both postgres container and `DB_CONNECTION_URI`.  |
| `DB_CONNECTION_URI`  | composed                  | Embeds `POSTGRES_PASSWORD`. Update both if you rotate it. |

Rotating `ENCRYPTION_KEY` requires a re-encryption migration — do not change it casually after data exists.
