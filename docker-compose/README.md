# docker-compose stacks

These stacks are **not** managed by ArgoCD — they run directly on a host's Docker
daemon. Which host a stack belongs to is decided by directory layout:

| Path | Host | Notes |
| --- | --- | --- |
| `<app>/` | `prd01` (192.168.219.100) | Historical flat layout. Also the k3s control-plane. |
| `pi01/<app>/` | `pi01` (192.168.219.127) | Raspberry Pi 4B, **arm64** — images must be multi-arch. |
| `media01/<app>/` | `media01` (192.168.219.191) | VM on `pve02`. Bulk storage on a ZFS mirror of **SMR** drives — read that host's README before adding anything that writes small and often. |

The flat top-level directories predate the split and are left in place because
moving them would change the deploy paths already in use on `prd01`. New stacks
go under a host directory.

One flat directory is on its way out: `nextcloud/` is being moved to
`media01/nextcloud/`, so that the largest body of data in the homelab stops
living on a single unmirrored disk. Until that migration is finished and
verified, `nextcloud/` remains the running instance — see
`media01/README.md` for the procedure and the order of the cutover steps.

## Why pi01 exists

`prd01` is simultaneously the k3s control-plane and the host of every stack in
the flat layout — including AdGuard Home, which serves DNS for the whole LAN.
Losing that one machine takes the network's name resolution with it.

`pi01` carries the pieces that are only useful when `prd01` is *down*, so it is
deliberately kept **outside** the k3s cluster and off any shared dependency:

- `pi01/adguard-home` — secondary DNS, config replicated from the primary.
- `pi01/uptime-kuma` — external probe; the only thing positioned to alert when
  the cluster itself is unreachable.
- `pi01/node-exporter` — metrics, scraped over the LAN by the in-cluster
  Prometheus (same pattern as the Mac mini target).

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
