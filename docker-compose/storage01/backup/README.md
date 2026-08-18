# backup — declarative homelab backup (host: storage01)

`config.yaml` declares what gets backed up. A Python runner on storage01 reads
it, pulls from each source, and — only on a fully successful run — asks pve02 to
snapshot the destination dataset.

Nothing here moves bytes itself. The runner assembles and supervises
`rsync`, `mongodump` / `mysqldump` / `pg_dump` / `mariadb-dump`, and `kubectl exec`.

## Why pull

storage01 reaches out to each source rather than each source pushing here.

A push design gives every source host write credentials to the backup store, so
one compromised or fat-fingered machine can destroy its own backups. Pull keeps
those credentials in one place, gives one scheduler and one log, and means a
source can be as dumb as an SSH endpoint.

The honest limit of that argument: pull makes **storage01** the machine with
write access to everything. That is why snapshot creation and snapshot
destruction are split — see below.

## The three sections

They are orthogonal on purpose.

| Section | Answers | Why separate |
| --- | --- | --- |
| `destination` | where does it land | Declared once. A per-job `dest:` inevitably drifts out of sync between jobs. |
| `sources` | how do I reach this host, and how do I read root-owned files there | This is the only place host-to-host variance is allowed to live. |
| `jobs` | what do I copy | References a source by name; carries everything that is a property of *the data*, not of the host. |

The split matters most for the last row. Excludes, dump consistency flags and
destination layout are **job** properties — two jobs on the same host routinely
need different ones — so pushing them into `sources` would collapse exactly the
distinction that makes the file readable.

## Retention is deliberately not in this file

`config.yaml` says when a snapshot is *created*. It says nothing about when one
is *destroyed*, and it must not.

The runner asks pve02 for a snapshot over SSH using a **forced-command key** that
can only run `zfs snapshot`. Pruning, and replication to `vault`, are driven by
pve02's own timer. So:

- a snapshot is always caused by a *completed* collection — an independent timer
  could fire mid-`rsync` and preserve a torn backup, which is worst precisely for
  jobs where a dump and its files must agree;
- and nothing running on storage01 — runner bug, bad config, or a compromise of
  the VM — can erase backup history.

If retention lived here, storage01 would hold both halves and the second
property would be lost.

## `sources`

| Mode | Meaning | Used by |
| --- | --- | --- |
| `privilege: sudo-rsync` | `rsync --rsync-path="sudo rsync"` over SSH | prd01 |
| `local: true` | runner's own host; no network hop | storage01 |
| `kubectl: {}` | no filesystem at all — dumps via `kubectl exec` | k3s |

### How prd01 is read without a password

prd01's `sudo` requires a password in general. `/etc/sudoers` grants exactly one
thing:

```
marshall ALL=(root) NOPASSWD: /usr/bin/rsync --server --sender *
```

`--server --sender` is rsync's send-only mode: it reads and transmits, and has no
code path that writes to the source host. Verified rather than assumed — `rsync`
push, `sudo cat /etc/shadow` and `sudo rsync --daemon` are all refused, while a
219 MB pull of a root-owned directory succeeds.

This is not an escalation. `marshall` is already in the `docker` group, which is
root-equivalent; the rule replaces that broad path with a narrow read-only one.

### Ownership is preserved without the runner being root

The runner uses `rsync --fake-super`, which stores the source's uid, gid and mode
in an extended attribute instead of applying them:

```
$ getfattr -n user.rsync.%stat --only-values .../config.php
100640 0,0 33:33          # mode 0640, uid 33, gid 33

$ ls -ln .../config.php
-rw-r----- 1 1000 1000    # the file itself belongs to the unprivileged runner
```

Restoring with `--fake-super` on the sending side reconstitutes real ownership.
This works because `tank` was created with `xattr=sa` and the virtiofs mount
carries `expose-xattr=1`; without both, ownership would silently degrade to the
runner's own uid.

The alternative — running the receiving `rsync` under `sudo` — would require
SSH keys and `known_hosts` under `/root` on storage01 and would hand the backup
process root for no benefit.

## `jobs`

```yaml
- name: blog              # directory name under destination.root
  source: prd01           # key from `sources`
  paths:
      - from: /mnt/hdd/data/blog-backend
        to: blog/files    # relative to destination.root
        exclude: [/db/]   # rsync filter rules, anchored with a leading /
  dumps:
      - to: blog/dump/mongo.archive.gz
        engine: mongodump
        container: blog-database
        args: [--archive, --gzip]
        auth:
            username_env: MONGO_INITDB_ROOT_USERNAME
            password_env: MONGO_INITDB_ROOT_PASSWORD
```

### `paths` — never copy a live database directory

Every job that has a database excludes that database's datadir and takes a
logical dump instead. An `rsync` of a running InnoDB or WiredTiger datadir
produces files that are individually complete and collectively meaningless.

`exclude` also exists for a second reason this repo has already been bitten by:
during the Nextcloud migration, `data/` lived *inside* the application directory,
and omitting the exclude would have dropped 19 GB onto a 100 GB root disk where a
later bind mount hid it — invisible, and still consuming the disk. Paths are not
self-describing; the excludes are part of the definition.

### `dumps` — the engine is not enough

Each entry names an engine *and* the flags that make it consistent, because the
right flags differ per engine and per deployment:

| Engine | What the config has to carry |
| --- | --- |
| `mysqldump` / `mariadb-dump` | `--single-transaction` (valid because these schemas are InnoDB), charset, and sometimes transport flags — the Nextcloud database needs `--protocol=socket --skip-ssl`, since the client honours `MYSQL_HOST` from the environment and would otherwise connect over TCP to itself where only `root@localhost` exists |
| `mongodump` | archive/compression flags and credentials, which the official image enforces once `MONGO_INITDB_ROOT_USERNAME` is set |
| `pg_dump` | user, database and password, all of which live in the container's environment |
| `sqlite` | a `path` instead of a `container`, a `method`, and a `tmp`. n8n's 5.8 GB store is SQLite in WAL mode: the main file, `-wal` and `-shm` are only meaningful together and only at an instant no writer is mid-commit, so it is excluded from `paths` and copied with `VACUUM INTO` — consistent against a running n8n, and compacting as a side effect. Nothing is installed for this: the runner pipes a short program to the source's existing `python3`, whose `sqlite3` module is stdlib |

Credentials are read from the container's own environment (`*_env` fields)
rather than duplicated into a secret file. **The variable names must match what
compose actually passed** — the MariaDB image does not alias `MYSQL_*` into
`MARIADB_*`, so referencing the wrong one yields an empty password and a
confusing "access denied".

### `precondition` — assumptions that must not rot silently

A job may declare a condition the runner checks before it will run:

```yaml
precondition:
    container_not_running: miniflux-db
```

This exists for exactly one situation so far. Miniflux's PostgreSQL datadir sits
on prd01 but the stack is not deployed, so nothing writes to it and an `rsync` of
the datadir is a valid *cold* copy. That reasoning is true only while it stays
stopped — the day it is started again, the same job silently becomes the
live-datadir copy this whole file exists to prevent.

The precondition turns that from a comment nobody re-reads into a failure. A
refused job is a message; a quietly corrupt backup is not.

### Every job writes a manifest

The runner always writes `manifest.json` next to a job's output — engine and
version, source host and paths, excludes applied, `--fake-super` on or off, byte
counts, and the command needed to restore. This is not optional and has no
config flag.

A ZFS snapshot on its own is a pile of files. Restoring a service needs the DB
dump *and* its files *and* the ownership *and* the knowledge that some directory
was deliberately excluded. Six months from now that context exists only if it was
written down at collection time.

## Prerequisites on the source hosts

Deliberately installed once rather than fetched at backup time — a backup that
depends on the network and a package registry at 3 a.m. is a backup that fails on
the night it is needed.

The list is short on purpose. A source host should be reachable, not
provisioned: no agent, no bespoke binary, nothing to keep in sync across
machines. Where source-side work is unavoidable — `rsync --server` for files,
a consistent read for SQLite — it runs on what the distribution already ships.

| Host | Needs | Why |
| --- | --- | --- |
| prd01 | the `/etc/sudoers` rule above | read root-owned files without a password |
| prd01 | storage01's public key in `~/.ssh/authorized_keys` | pull direction; the earlier migration only opened prd01 → storage01 |
| storage01 | `python3` + `pyyaml`, `rsync`, `attr` | the runner itself; all present |
| pve02 | a forced-command key permitting only `zfs snapshot` | see "Retention" above |

## Restore

Snapshots are browsable directly — no backup format, nothing to corrupt, no tool
required to read them:

```sh
# on pve02
ls /tank/backup/hosts/.zfs/snapshot/
cat /tank/backup/hosts/.zfs/snapshot/<stamp>/blog/manifest.json
```

Files go back with ownership intact by putting `--fake-super` on the *sending*
side:

```sh
rsync -aHAX --numeric-ids --fake-super \
  /mnt/backup/hosts/.zfs/snapshot/<stamp>/blog/files/ \
  marshall@192.168.219.100:/mnt/hdd/data/blog-backend/
```

Databases are restored from their dumps, per the command recorded in the
manifest.

## What is deliberately not backed up

- **Bulk data already on `tank`** — Nextcloud files, Immich originals, the
  Jellyfin library. Copying `tank` to `tank` buys nothing. What protects it is
  the `vault` replica and the offsite copy, both driven from pve02.
- **Derived data** — Immich thumbnails and transcodes, Nextcloud previews.
  Regenerated from the originals; backing them up trades real space for time
  that is cheap to spend again.
- **`/mnt/hdd/data/cloud` on prd01 (37 GB)** — a Nextcloud installation two
  generations old. It is reclaimable space, not a backup target.
- **`/mnt/hdd/data/nextcloud` on prd01 (21 GB)** — superseded by the migration to
  storage01 on 2026-08-18. Kept only as a rollback path; delete it once the new
  instance has been exercised.

## Still missing

**Offsite.** `tank` is one mirror in one machine and `vault` is a second disk in
the same machine. Neither survives fire, theft, or a mistake that reaches both.
The database dumps total roughly 1 GB, so an offsite copy of at least those costs
almost nothing — this is the largest remaining gap in the 3-2-1 story, not a
nice-to-have.
