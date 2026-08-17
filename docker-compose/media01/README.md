# media01 — storage and media VM (192.168.219.191)

Debian 13 (trixie), amd64, 12 vCPU / 20 GiB RAM, 100 GB root on NVMe.
A QEMU guest on **pve02** (`192.168.219.199`), not a physical machine.

This host exists because pve02 is where the bulk storage is. Everything here is
arranged around one hardware fact, so it is worth stating before the stack list:

> The 8 TB pair backing `tank` is **`ST8000DM004` — DM-SMR**. `smartctl` reports
> the model family as `Seagate BarraCuda 3.5 (SMR)` directly, so this is not an
> inference.

## What SMR does and does not cost

Measured on the pool before anything was deployed, with incompressible data:

| Workload | Result |
| --- | --- |
| 12 GiB sequential write | 175 MB/s |
| 12 GiB sequential read | 199 MB/s |
| **35 GiB sustained write** (7 x 5 GiB) | **129-161 MB/s, no cliff** |

35 GiB is past the drives' CMR cache zone (typically 20-25 GB), and throughput
never collapsed. `recordsize=1M` plus ZFS transaction groups turn writes into
large sequential batches, which is the one thing DM-SMR handles well.

What *does* destroy these drives is the opposite pattern — a stream of small
random writes forcing read-modify-write. Databases, thumbnail generation and
transcoding scratch are all exactly that. So the rule every stack here follows:

**Bulk sequential data on the mirror; anything that writes small and often on
the NVMe.**

| Path | Backing | Holds |
| --- | --- | --- |
| `/var/lib/homelab/<app>` | NVMe (VM root disk) | databases, caches, thumbnails, transcode scratch |
| `/mnt/media` | virtiofs -> `tank/media` | Jellyfin library |
| `/mnt/photos` | virtiofs -> `tank/photos` | Immich originals |
| `/mnt/files` | virtiofs -> `tank/files` | Nextcloud user files |

This mirrors pi01's `/var/lib/homelab/<app>` convention, for a different reason:
there it was an unfit exFAT disk, here it is write *pattern* rather than write
volume.

## Why virtiofs and not a virtual disk

The three `/mnt/*` mounts are host ZFS datasets exposed straight into the guest
by `virtiofsd`, configured on pve02 as directory mappings (`/etc/pve/mapping/directory.cfg`)
and attached as `virtiofs0..2`.

The obvious alternative — hand the VM a zvol and put a filesystem on it — stacks
a guest filesystem on top of ZFS on top of SMR, so the guest's allocator and
ZFS's both make placement decisions neither can see. virtiofs keeps a single
filesystem in the path, and the host keeps its checksums and snapshots over data
the guest is writing.

Cost measured from inside the guest: **132 MB/s write** (against 175 MB/s
natively on the host, ~75%) and 327 MB/s read, the latter inflated by page
cache. The disks remain the bottleneck, not the transport.

Mounts are in the guest's `/etc/fstab` with `nofail`, verified to survive a
reboot:

```
media   /mnt/media   virtiofs  defaults,nofail  0 0
photos  /mnt/photos  virtiofs  defaults,nofail  0 0
files   /mnt/files   virtiofs  defaults,nofail  0 0
```

## Stacks

| Stack | Port | Notes |
| --- | --- | --- |
| `jellyfin` | 8096 | Library read-only from `/mnt/media`. CPU transcoding. |
| `immich` | 2283 | iPhone photo backup. Thumbnails/transcodes forced onto NVMe. |
| `nextcloud` | 8080 | Migrated off prd01. Public at `cloud.marshallku.dev`. |

Immich rather than Nextcloud for phone photo backup: they overlap, but Immich's
iOS background upload is the part that actually has to work unattended, and it
is the reason this host was built. Nextcloud stays for general file sync.

### GPU

pve02 has a GTX 1060 3 GB. It is **not** passed through. PCIe passthrough is
per-VM and exclusive, so the card would belong to media01 alone — which is
acceptable, since every consumer (Jellyfin transcoding, Immich ML) lives in this
one VM. It is simply not needed yet: 36 host threads cover several software
1080p transcodes, and iOS clients direct-play most files.

Two things worth recording, because both were initially got wrong:

- Passthrough does **not** require removing the card. The BIOS still uses it to
  POST; `vfio-pci` only claims it after boot. The "X79 won't POST without a GPU"
  note in the hardware roadmap is about *physically pulling* the card and does
  not apply.
- What passthrough actually costs is the host's console after boot. pve02 has no
  onboard VGA and no IPMI, so that console disappears entirely.

## Setup

```sh
git clone https://github.com/marshallku/manifest.git ~/dev/manifest
cd ~/dev/manifest/docker-compose/media01

sudo mkdir -p /var/lib/homelab/{jellyfin/{config,cache},immich/{postgres,thumbs,encoded-video},nextcloud/{db,html,redis,appdata}}
sudo chown -R 1000:1000 /var/lib/homelab/jellyfin

cd jellyfin && docker compose up -d

cd ../immich
cp .env.example .env && chmod 600 .env
$EDITOR .env                # DB_PASSWORD: openssl rand -base64 32
docker compose up -d        # picks up docker-compose.override.yml automatically
```

`immich/docker-compose.yml` is vendored verbatim from the upstream release so it
can be diffed against a new one. To update it, re-download from
`https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml`,
read the diff, and bump `IMMICH_VERSION` in `.env` deliberately — never leave it
on the floating `release` tag.

## Nextcloud migration from prd01

Not a fresh install. The instance, its database and its 19 GB of user data move
from prd01 (`/mnt/hdd/data/nextcloud`, a single unmirrored disk) onto `tank`.
Versions are pinned to what prd01 ran — **move first, upgrade later**, so a
failure is unambiguous.

```sh
# --- on prd01: quiesce and dump -------------------------------------------
cd ~/dev/manifest/docker-compose/nextcloud
docker exec -u www-data nextcloud-app-1 php occ maintenance:mode --on
docker exec nextcloud-db-1 sh -c \
  'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction \
     --default-character-set=utf8mb4 clouddb' > /tmp/clouddb.sql

# --- copy: application dir, then user data --------------------------------
# `--exclude data/` is load-bearing. The data directory lives *inside* the
# application directory on prd01, so without it this first pass would drop 19 GB
# onto a 100 GB root disk, where the /mnt/files bind mount would then hide it —
# invisible, and still consuming the disk.
rsync -aHAX --info=progress2 --exclude 'data/' \
  /mnt/hdd/data/nextcloud/app/  media01:/var/lib/homelab/nextcloud/html/

# The preview cache goes to the NVMe, matching the nested mount in
# docker-compose.yaml; everything else in data/ goes to the mirror.
rsync -aHAX --info=progress2 \
  /mnt/hdd/data/nextcloud/app/data/appdata_ocglba52vmd1/  media01:/var/lib/homelab/nextcloud/appdata/
rsync -aHAX --info=progress2 --exclude 'appdata_ocglba52vmd1/' \
  /mnt/hdd/data/nextcloud/app/data/  media01:/mnt/files/

# Run the bulk pass once with the instance still live, then again after
# maintenance mode is on, so the unavoidable downtime is only the second
# (near-empty) pass.

# --- on media01: restore --------------------------------------------------
cp .env.example .env && chmod 600 .env    # MYSQL_* copied from prd01's .env
docker compose up -d db
docker exec -i nextcloud_db sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" clouddb' < clouddb.sql
docker compose up -d

# The restored config.php trusts only cloud.marshallku.dev. NEXTCLOUD_TRUSTED_DOMAINS
# does NOT fix this — the entrypoint applies it at install time, and this is not
# an install. Without this line the LAN address returns "untrusted domain" and
# there is no way in until the tunnel is repointed.
docker exec -u www-data nextcloud_app \
  php occ config:system:set trusted_domains 1 --value=192.168.219.191

docker exec -u www-data nextcloud_app php occ maintenance:mode --off
docker exec -u www-data nextcloud_app php occ files:scan --all
```

The database credentials must be prd01's, not new ones — the restored
`config.php` names `clouddb`/`clouduser` and will not be rewritten.

### Post-migration, in order

1. **Repoint the tunnel.** `cloud.marshallku.dev` is served through the
   Cloudflare tunnel from prd01. Nothing in this directory moves it; the
   ingress has to be changed to `192.168.219.191:8080`. Until then the instance
   is only reachable on the LAN address added by the `occ` step above.
2. **Enable Redis.** The container is already running but unused. Add
   `REDIS_HOST=redis` to `.env` and restart — the image's
   `config/redis.config.php` reads it through `getenv()` at runtime, so no
   `config.php` editing is involved. Left out of the migration itself so that
   the move changes location and nothing else.
3. **Retire prd01's stack** only once both of the above are done and the
   instance has been exercised — then remove `docker-compose/nextcloud/` and
   free `/mnt/hdd/data/nextcloud`.

The preview cache is *not* on this list: the nested mount in
`docker-compose.yaml` already places `appdata_ocglba52vmd1` on the NVMe from the
first start, and the migration copies it there directly.

## Host-side notes (pve02)

Things this directory cannot configure, recorded so they are not rediscovered:

- **DNS is `192.168.219.100` then `192.168.219.127`** — the same DNS1/DNS2 pair
  documented for pi01. The router at `.1` does **not** answer DNS; pointing
  cloud-init at it produces a VM with working routing and no name resolution,
  which looks like a broken network and is not.
- The VM is `onboot=1` with `qemu-guest-agent` installed, so pve02 can shut it
  down gracefully and report its address.
- `balloon=0`. virtiofs requires shared memory backing, and ballooning fights
  that.
- Storage on pve02 is registered so that **`tank` cannot hold VM disks**: it is a
  `dir` storage limited to `backup,iso,vztmpl`, while `vault` (a single 4 TB CMR
  disk) carries `images,rootdir`. "Don't put VM disks on SMR" is enforced by the
  storage definition rather than by remembering it.
