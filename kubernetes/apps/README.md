# apps/ — the factory floor

Each subdirectory here is **one app**, described by a single `values.yaml` for the
[`workload`](../charts/workload/README.md) chart. The ArgoCD **ApplicationSet**
([`../applicationset/factory-apps.yaml`](../applicationset/factory-apps.yaml))
watches this directory and creates one ArgoCD `Application` per subdirectory —
**no manual `kubectl apply` per app.**

## Add an app

```
kubernetes/apps/<name>/values.yaml
```

Commit it. Within a couple of minutes ArgoCD generates `Application/<name>`,
deploys it to namespace `<name>`, and (for any surface with a `host`) the
Cloudflare Tunnel ingress controller wires the public subdomain + DNS.

See [`../charts/workload/README.md`](../charts/workload/README.md) for the full
values contract. Minimal example:

```yaml
app: <name>
surfaces:
  web:
    image: ghcr.io/marshallku/<name>-web:latest
    port: 3000
    host: <name>.marshallku.dev
```

## Namespace prerequisites

Before an app that pulls private images or uses Infisical secrets syncs cleanly,
its namespace needs the bootstrap secrets (`ghcr-secret`,
`infisical-universal-auth`). The `/new-app` command (Phase 2) seeds these; until
then add them once per namespace by hand.
