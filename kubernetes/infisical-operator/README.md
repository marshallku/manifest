# Infisical Operator

Kubernetes operator that reconciles `InfisicalSecret` (and related) CRs into native `Secret` resources by pulling values from the Infisical instance in `kubernetes/infisical/`.

Rendered from `infisical-helm-charts/secrets-operator` chart `v0.10.33` with these overrides:

- `hostAPI=http://infisical.infisical.svc.cluster.local:8080/api` — points the operator at the in-cluster Infisical instead of `app.infisical.com`.
- `controllerManager.nodeSelector.kubernetes.io/hostname=mgmt01` — pinned alongside Infisical itself.
- `scopedNamespaces=[maji-dev]` + `scopedRBAC=true` — the operator only watches and writes into `maji-dev`. The manager runs with a namespaced `Role` (in `maji-dev`), not a `ClusterRole`, so it cannot mutate Secrets anywhere else. To extend coverage to another namespace, add it to `scopedNamespaces` and re-render.

## Layout

```
infisical-operator/
├── namespace.yaml
├── crds/
│   ├── infisicalsecret-crd.yaml         # the one we use
│   ├── infisicalpushsecret-crd.yaml
│   ├── infisicaldynamicsecret-crd.yaml
│   └── clustergenerator-crd.yaml
├── rbac/
│   ├── serviceaccount.yaml
│   ├── manager-rbac.yaml                # Role + RoleBinding scoped to maji-dev (scopedRBAC)
│   ├── leader-election-rbac.yaml
│   ├── metrics-auth-rbac.yaml           # ClusterRole — only for /metrics endpoint authn, no secret access
│   └── metrics-reader-rbac.yaml
├── services/metrics-service.yaml
└── deployments/deployment.yaml          # 1 replica, mgmt01
```

## Apply

ArgoCD `Application` (recommended):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: infisical-operator
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/marshallku/manifest.git
    targetRevision: HEAD
    path: kubernetes/infisical-operator
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: infisical-operator
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```

Or directly:

```sh
kubectl apply -f kubernetes/infisical-operator/namespace.yaml
kubectl apply -R -f kubernetes/infisical-operator/
```

## Updating the chart

```sh
helm repo update infisical-helm-charts
helm template infisical-secrets-operator infisical-helm-charts/secrets-operator \
  --version <new-version> \
  --namespace infisical-operator \
  --include-crds \
  --set hostAPI=http://infisical.infisical.svc.cluster.local:8080/api \
  --set 'controllerManager.nodeSelector.kubernetes\.io/hostname=mgmt01' \
  --set 'scopedNamespaces[0]=maji-dev' \
  --set scopedRBAC=true \
  --output-dir /tmp/render
```

Then diff `/tmp/render/secrets-operator/templates/*` against the files here and merge. Re-add `namespace: infisical-operator` to namespaced resources whose metadata is missing it (Deployment, ServiceAccount, leader-election Role/RoleBinding, metrics Service) — the chart relies on the `kubectl -n` / Argo destination for these.
