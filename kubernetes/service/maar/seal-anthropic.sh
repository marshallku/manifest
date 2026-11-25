#!/usr/bin/env bash
# Seal the Anthropic API key for MAAR's "AI로 읽기" into maar-secret (ANTHROPIC_API_KEY) and apply it.
# Run it yourself in a terminal: the key is typed at a hidden prompt and goes straight into kubeseal — it is never
# printed, written to disk or passed as an argument.
#   bash ~/dev/manifest/kubernetes/service/maar/seal-anthropic.sh
# Then add ANTHROPIC_API_KEY to secretEnv in kubernetes/apps/maar/values.yaml (after this, never before: the pod needs
# the key to exist) and commit both files.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cert=$(mktemp)
trap 'rm -f "$cert"' EXIT
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > "$cert"
read -rsp "Anthropic API key: " key
echo
[[ -n "$key" ]] || { echo "✗ no key entered" >&2; exit 1; }
# Only its digest is kept to recognise the new value in the live Secret (a rotation must not pass on the old key).
want=$(printf %s "$key" | shasum -a 256 | cut -d' ' -f1)
kubectl create secret generic maar-secret -n maar \
  --from-file=ANTHROPIC_API_KEY=<(printf %s "$key") --dry-run=client -o yaml \
  | kubeseal --cert "$cert" --format yaml --merge-into "$here/sealed-secret.yaml"
unset key
kubectl apply -f "$here/sealed-secret.yaml" >/dev/null
# The controller unseals asynchronously: wait (bounded) until the key is in the Secret.
for _ in $(seq 1 30); do
  # Not there yet (NotFound) is one more attempt, not a failure.
  have=$( (kubectl -n maar get secret maar-secret -o jsonpath='{.data.ANTHROPIC_API_KEY}' 2>/dev/null || true) \
    | base64 -d 2>/dev/null | shasum -a 256 | cut -d' ' -f1) || have=""
  if [[ "$have" == "$want" ]]; then
    echo "✓ ANTHROPIC_API_KEY is in maar-secret — now add it to secretEnv in kubernetes/apps/maar/values.yaml"
    exit 0
  fi
  sleep 1
done
echo "✗ maar-secret does not hold the new ANTHROPIC_API_KEY after 30s (check the sealed-secrets controller)" >&2
exit 1
