# docker-compose stacks

These stacks are **not** managed by ArgoCD — they run directly on a host's Docker
daemon. Which host a stack belongs to is decided by directory layout:

| Path | Host | Notes |
| --- | --- | --- |
| `<app>/` | `app01` (192.168.219.194) | Historical flat layout, inherited from `prd01` on 2026-09-09. Also the main AdGuard Home. |
| `pi01/<app>/` | `pi01` (192.168.219.127) | Raspberry Pi 4B, **arm64** — images must be multi-arch. |
| `storage01/<app>/` | `storage01` (192.168.219.191) | VM on `pve02`. Bulk storage on a ZFS mirror of **SMR** drives — read that host's README before adding anything that writes small and often. |

The flat top-level directories predate the split and are left in place because
moving them would change the deploy paths already in use on `app01` (which kept
`prd01`'s `/mnt/hdd/data/<app>` layout verbatim through the move, so that the
stacks and their bind mounts did not have to change at once). New stacks go
under a host directory.

One flat directory is dead: `nextcloud/` **was migrated** to
`storage01/nextcloud/` (2026-08-18), so that the largest body of data in the
homelab no longer lives on a single unmirrored disk. `cloud.marshallku.dev` is
served from storage01, and the flat `nextcloud/` stack was kept only as a
rollback path onto `prd01` — which was powered off on 2026-09-09 and is not
coming back. The rollback it existed for is no longer possible, so the directory
is now pure dead weight and should be deleted. See `storage01/README.md` for
what was done.

## Why pi01 exists

`app01` hosts every stack in the flat layout — including AdGuard Home, which
serves DNS for the whole LAN — and `k3s01` is a single-node cluster with no
second control-plane. Both are guests on the same hypervisor, `pve02`, so losing
one machine can take the network's name resolution with it.

`pi01` carries the pieces that are only useful when `app01`, `k3s01` or `pve02`
itself are *down*. It is a physical Raspberry Pi, deliberately kept **outside**
the k3s cluster, off `pve02`, and off any shared dependency:

- `pi01/adguard-home` — secondary DNS, config replicated from the primary.
- `pi01/uptime-kuma` — external probe; the only thing positioned to alert when
  the cluster itself is unreachable.
- `pi01/node-exporter` — metrics, scraped over the LAN by the in-cluster
  Prometheus (same pattern as the Mac mini target).
- `pi01/homelab-status` — collapses Kuma and Prometheus into one small JSON
  document for the ESP32-S3 shelf display. Here rather than on `app01` for the
  same reason as `uptime-kuma`: it has to keep answering when `pve02` does not.

### Storage on pi01

Everything lives on the SD card (`/var/lib/homelab/<app>`), **not** on
`/mnt/hdd`. Two independent reasons:

1. `/mnt/hdd` is **exFAT** — no POSIX permissions, symlinks, hardlinks or
   ownership. `chmod 600` silently stays `755`, so a config file holding
   credentials cannot be protected, and Docker's overlayfs cannot use it at all.
2. The drive is a 2010-era ST9500325AS reporting **90 reallocated sectors** over
   10k power-on hours. Putting the failover DNS on it would mean the backup
   depends on hardware less healthy than what it is backing up.

SD write volume is instead bounded by *configuration* — short query-log and
statistics retention on the AdGuard replica, and the `10m x 3` container log cap
already set in `/etc/docker/daemon.json`.
