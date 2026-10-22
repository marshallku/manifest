#!/usr/bin/env python3
"""argocd-regen.py — regenerate kubernetes/argocd from an upstream install.yaml.

Usage:
    curl -sfLo /tmp/install.yaml \
        https://raw.githubusercontent.com/argoproj/argo-cd/<tag>/manifests/install.yaml
    python3 scripts/argocd-regen.py kubernetes/argocd /tmp/install.yaml write   # or: check

Each file under kubernetes/argocd that holds exactly one upstream object is
rewritten from the given upstream version, and `customize()` re-applies the
local deltas — that function is the complete list of how this install differs
from upstream. Local-only objects (AppProjects, the Namespace, the NodePort
service) and upstream objects never vendored here (dex, notifications, most
NetworkPolicies) are left alone.

Apply afterwards: CRDs with `kubectl apply --server-side --force-conflicts`
(the applicationsets CRD is past the client-side annotation limit), the rest
with plain `kubectl apply`. Not `secrets/`: argocd-server owns that data.

Used for v3.3.5 -> v3.5.6 on 2026-10-22. Regenerating at v3.3.5 first was
semantically identical to the hand-maintained files, apart from the repo-server
copyutil `ln -sf` fix (upstream has it since) and two upstream labels.
"""
import glob, re, sys, yaml

class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

def str_presenter(dumper, data):
    # Match the Prettier formatting of the files being replaced: block style
    # for multi-line strings, double quotes wherever a plain scalar won't do.
    if "\n" in data:
        style = "|"
    else:
        analysis = dumper.analyze_scalar(data)
        plain_ok = analysis.allow_block_plain and not analysis.empty
        resolves_to_str = dumper.resolve(yaml.ScalarNode, data, (True, False)) == "tag:yaml.org,2002:str"
        style = None if plain_ok and resolves_to_str else '"'
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)
Dumper.add_representer(str, str_presenter)

def customize(o):
    kind, name = o["kind"], o["metadata"]["name"]
    if kind not in ("ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition"):
        o["metadata"]["namespace"] = "argocd"
    if (kind, name) == ("ConfigMap", "argocd-cm"):
        o.setdefault("data", {}).update({"accounts.sssup": "apiKey, login", "url": "https://argocd.marshallku.dev"})
    if (kind, name) == ("ConfigMap", "argocd-cmd-params-cm"):
        o["data"] = {**(o.get("data") or {}), "server.insecure": "true"}
    if (kind, name) == ("ConfigMap", "argocd-rbac-cm"):
        o["data"] = {**(o.get("data") or {}), "policy.csv": RBAC}
    if (kind, name) == ("Deployment", "argocd-repo-server"):
        # `var-files` is an emptyDir, which survives restarts and host reboots,
        # so copyutil reruns against its own previous output. With a plain
        # `ln -s` (upstream up to v3.3.5) that exits 1 → CrashLoopBackOff →
        # every Application SYNC Unknown; a 2-day outage on 2026-07-31.
        # Upstream is idempotent since; refuse to regenerate if that regresses.
        copyutil = next(c for c in o["spec"]["template"]["spec"]["initContainers"] if c["name"] == "copyutil")
        if "ln -sf" not in " ".join(copyutil.get("args", [])):
            sys.exit("copyutil is no longer idempotent upstream: re-add `ln -sf` before applying")
    if (kind, name) == ("Deployment", "argocd-applicationset-controller"):
        for c in o["spec"]["template"]["spec"]["containers"]:
            c["imagePullPolicy"] = "IfNotPresent"
    return o

root, upstream, mode = sys.argv[1], sys.argv[2], sys.argv[3]
up, up_text = {}, {}
for segment in re.split(r"(?m)^---\n", open(upstream).read()):
    d = yaml.safe_load(segment)
    if d:
        up[(d["kind"], d["metadata"]["name"])] = d
        up_text[(d["kind"], d["metadata"]["name"])] = segment
RBAC = None
drift = False
for p in sorted(glob.glob(root + "/**/*.yaml", recursive=True)):
    docs = [d for d in yaml.safe_load_all(open(p)) if d]
    if len(docs) == 1 and (docs[0]["kind"], docs[0]["metadata"]["name"]) == ("ConfigMap", "argocd-rbac-cm"):
        RBAC = docs[0]["data"]["policy.csv"]
for p in sorted(glob.glob(root + "/**/*.yaml", recursive=True)):
    docs = [d for d in yaml.safe_load_all(open(p)) if d]
    if len(docs) != 1: print("SKIP multi", p); continue
    key = (docs[0]["kind"], docs[0]["metadata"]["name"])
    if key not in up: continue
    if key[0] == "CustomResourceDefinition":
        # Verbatim, so that the next upgrade's diff is upstream's diff. CRDs
        # are cluster-scoped and carry no local delta.
        out = up_text[key]
    else:
        out = yaml.dump(customize(up[key]), Dumper=Dumper, sort_keys=False, width=10**6, allow_unicode=True)
    if mode == "check":
        if open(p).read() != out:
            print("DIFFERS", p)
            drift = True
    else:
        open(p, "w").write(out)
sys.exit(1 if drift else 0)
