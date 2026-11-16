#!/usr/bin/env bash
# Provision maar's database on the shared homelab Postgres and seal DATABASE_URL into maar-secret.
# Run it yourself in a terminal outside Claude Code: the URL carries the password and goes straight from
# provision-db.sh into kubeseal — it is never printed, written to disk or passed as an argument.
#   bash ~/dev/manifest/kubernetes/service/maar/seal-db.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="$(cd "$here/../../.." && pwd)"
cert=$(mktemp)
trap 'rm -f "$cert"' EXIT
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"
"$manifest/scripts/provision-db.sh" maar \
  | sed -n 's/^DATABASE_URL=//p' | tr -d '\n' \
  | kubectl create secret generic maar-secret -n maar --from-file=DATABASE_URL=/dev/stdin --dry-run=client -o yaml \
  | kubeseal --cert "$cert" --format yaml > "$here/sealed-secret.yaml"
echo "sealed → $here/sealed-secret.yaml (commit it; then kubectl apply -f $here/)"
