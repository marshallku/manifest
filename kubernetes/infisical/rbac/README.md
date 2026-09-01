# `backup-runner` — cluster access for the storage01 backup runner

The nightly backup runner on storage01 dumps `infisical-postgres-0` with a
single call:

```sh
kubectl exec -n infisical infisical-postgres-0 -c postgres -- <pg_dump …>
```

storage01 is not a cluster member and has no business becoming one — it is the
backup target, and a backup target that belongs to the cluster it backs up
shares that cluster's failure domain. So it authenticates to the API instead,
as this ServiceAccount.

## Why not a SealedSecret

SealedSecrets move a secret *from git into the cluster*. Here the consumer is
*outside* the cluster, so the direction is wrong. Nothing in this directory is
secret: the ServiceAccount, Role and RoleBinding are plain authorization
objects, and the token Secret is committed with **no `data:` block** — the
cluster's token controller supplies the value. The token itself is delivered
out of band, exactly like the `backupsnap` SSH key on pve02.

## Files

| File | Contents |
|---|---|
| `serviceaccount-backup-runner.yaml` | ServiceAccount **+ its token Secret**, in that order |
| `role-backup-runner.yaml` | `pods get` and `pods/exec create`, both pinned to `infisical-postgres-0` |
| `rolebinding-backup-runner.yaml` | binds the two |

The account and its token Secret share a file on purpose. `kubectl apply -f`
feeds files alphabetically, which puts any `secret-*.yaml` ahead of
`serviceaccount-*.yaml`; the token controller then sees a Secret naming a
ServiceAccount that does not exist yet and **deletes it**. Apply still reports
success, so the cluster silently ends up with no token and the failure only
appears later as an authentication error in the nightly run. This was observed
here, not hypothesised.

## Apply

```sh
kubectl apply -f kubernetes/infisical/rbac/
```

## Verify the scope — do not skip this

The Role pins `pods/exec` with `resourceNames`. RBAC normally ignores
`resourceNames` on a `create` verb, since the object name does not exist yet at
authorization time; `pods/exec` is the exception, because it is a subresource
of an already-named pod and the name is in the request path. That exception is
load-bearing — without it this Role would grant exec into **every** pod in the
namespace — so confirm it against the live cluster after any change:

```sh
SA=system:serviceaccount:infisical:backup-runner

kubectl auth can-i create pods/infisical-postgres-0 --subresource=exec --as=$SA -n infisical   # yes
kubectl auth can-i create pods/infisical-redis-…    --subresource=exec --as=$SA -n infisical   # no
kubectl auth can-i create pods                      --subresource=exec --as=$SA -n infisical   # no
kubectl auth can-i list pods                                           --as=$SA -n infisical   # no
kubectl auth can-i get  pods/infisical-postgres-0                      --as=$SA -n infisical   # yes
kubectl auth can-i get  secrets                                        --as=$SA -n infisical   # no
```

Verified end to end on 2026-08-25 with a kubeconfig built from the token:
`pg_dump --version` ran inside the target pod, while exec into the redis pod,
`get pods`, `get secrets` and any access to `kube-system` all returned
`Forbidden`.

## Hand the token to the runner

Done 2026-09-01. Kept here because it is the procedure to repeat if the token is
ever rotated or the runner moves hosts.

```sh
kubectl -n infisical get secret backup-runner-token -o jsonpath='{.data.token}'    | base64 -d
kubectl -n infisical get secret backup-runner-token -o jsonpath='{.data.ca\.crt}'  | base64 -d
```

Build a kubeconfig from those two values pointing at `https://192.168.219.100:6443`,
place it at `/etc/backup/kubeconfig` (mode `0600`) on the host that runs
`kubectl` for the runner, and point the job at it with `KUBECONFIG`. The runner
never puts the credential in argv — the same invariant it keeps for database
passwords.

On storage01 the file is `root:root`, unreadable by the `marshall` account the
runner runs as; `backup.service` passes it in with `LoadCredential=` so that a
private copy exists only for the duration of a run. `kubectl` itself came from
the pkgs.k8s.io v1.34 apt repo, matching the cluster's minor.

The scope table above was re-verified against the live cluster from storage01
with this kubeconfig on 2026-09-01: all eight checks matched, and `pg_dump
--version` ran inside `infisical-postgres-0`.

## Then remove the admin credential

`marshall@prd01` currently holds `~/.kube/config` containing a **`system:masters`**
client certificate (valid until 2027-02-07). While that file exists, anything
running as `marshall` can ignore this Role entirely, which makes the whole
directory decorative. Delete it once the scoped kubeconfig above is in place.
