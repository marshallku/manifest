# dongjoo.me

WordPress + remark42, on `app01`. Rebuilt from scratch on **2026-09-29** after
the 2026-08-19 compromise. This file exists because almost nothing here is the
obvious choice, and the reasons are not recoverable from the compose file.

## What happened

`xmlrpc.php` brute force → admin login → `wp-admin/update.php?action=upload-plugin`
→ webshell. **Not a core CVE**: the password was guessed, so keeping WordPress
patched would not have prevented it. Discovered 2026-08-19, container stopped
the same day, left stopped for six weeks.

The old document root was a total loss — eight fake plugins (`wp2shell-*`,
`wp-optimizer-*`, `site-tweaks-*`, `admin-utils-*`), `wp-file-manager`
(CVE-2020-25213), webshells at three directory levels, a 135 KB `wp-conffg.php`
typosquatting `wp-config.php`, and an unexplained `jaida` theme. It was
deliberately not carried to `app01` during the 2026-09-09 move.

The **database**, by contrast, was almost clean: no injected options, no
attacker tables beyond `wp_wpfm_backup`, no content injection in any published
post. The damage was 48 administrator accounts, `active_plugins` naming the
attack tooling, and 412 unapproved spam comments.

## What the rebuild actually did

Fresh everything, with content carried across as data rather than as files:

| | |
|---|---|
| Carried | 12 posts, 2 pages, 61 attachments, 4 categories, 42 tags, 1 menu, the `Social Links` reusable block, site identity, permalinks, Yoast settings, `dorp` theme mods |
| Dropped | 48 attacker users, 412 spam comments, `wp_wpfm_backup`, the `Scripts` reusable block (an unreferenced GA tag; GA is carried by the `dorp` theme mod instead), `wp_template`/`wp_template_part`/`wp_global_styles` (leftovers from a block theme — `dorp` is classic), 3 tags attached to nothing |
| Rebuilt | WordPress core, MySQL instance and datadir, the MySQL root and application passwords, the WordPress admin password, the remark42 `SECRET` (the OAuth client secrets are **not** rotated — see the end of this file) |

Media was recovered from `pve02:tank/backup/hosts@backup-20260907T181740Z`
(held under the tag `dongjoo-rebuild-20260929` so retention cannot prune it) by
**extension allowlist over `uploads/2024` and `uploads/2025` only**, which
excludes by construction rather than by deletion: `kir.php`, `kerang.php`,
`2025/05/wp/OoqDzLit.php`, `wp-file-manager-pro/`, every `.htaccess`, and all of
`uploads/2026` (which held no media at all, only `.htaccess`). `svg` is not in
the allowlist, and none of the 61 attachments was one.

What was actually checked on the staged media: all 290 files' detected type
matched their extension, and none contained a PHP open tag. Neither proves the
absence of a polyglot — a valid image can still carry embedded content — so the
load-bearing control is the edge denying execution under `uploads`, not the
scan.

Verified after import: attachment ID → file mapping identical to the original
for 61/61, `_thumbnail_id` mapping byte-identical, 191 image URLs across the 12
posts all 200, 65 `srcset` attributes still emitted, `wp_users` = 1,
`wp_comments` = 0.

## Why the pieces are shaped the way they are

**Why a new MySQL instance and not just a new schema.** The previous compose
gave the WordPress container `env_file: .env`, and that file holds
`MYSQL_ROOT_PASSWORD`. A webshell in that container could read it from
`/proc/self/environ`, so every credential on the old instance is disclosed.
Auditing the old server (accounts, events, triggers, routines, loadable
functions, components, persisted variables — all clean, as it happens) answers
a narrower question than replacing it does.

**Hence the env split.** `.env.db`, `.env.wordpress` and `.env.remark`: a
service is handed only the secrets it uses. WordPress never sees the root
password or the remark42 OAuth secrets again.

**Why the service is `mysql`, not `db`.** Compose would otherwise adopt the
retired `dongjoome-db-1` container, which still holds the compromised datadir.

**Why `dns_search: "."`.** The container search domain is `marshallku.dev`,
which has a public `*` record, so an AAAA lookup for a bare service name like
`mysql` resolves to **Cloudflare**. IPv4 wins today only because Docker's
resolver answers the A query first. Removing the search domain removes the
whole collision class.

**Why `DISALLOW_FILE_MODS`.** It denies exactly the capability the break-in
used. Verified: an authenticated `dongdu` session gets 403 on
`plugin-install.php`, and `install_plugins`, `edit_plugins`, `update_plugins`
and `edit_themes` all return false.

## Updating plugins

`DISALLOW_FILE_MODS` blocks the admin UI, but the constant lives in
`WORDPRESS_CONFIG_EXTRA` and so only applies to the web container. A wp-cli
container run without that variable is unaffected:

```sh
cd ~/dev/manifest/docker-compose/dongjoo.me
sudo docker run --rm --network dongjoome_default --dns-search=. \
  -v /mnt/hdd/data/dongjoo.me/html:/var/www/html \
  -v /mnt/hdd/data/dorp:/var/www/html/wp-content/themes/dorp \
  -u 33:33 -e HOME=/tmp -e WP_CLI_PHP_ARGS=-dmemory_limit=1024M \
  --env-file .env.wordpress -e WORDPRESS_DB_HOST=mysql:3306 \
  wordpress:cli wp plugin update --all
```

Two things that will bite:

- The wp-cli image's entrypoint only prepends `wp` when the first argument
  starts with `-`. Write `wordpress:cli wp <command>`, not `wordpress:cli <command>`.
- `wp db <anything>` fails against MySQL 9: the image ships MariaDB's client,
  which cannot do `caching_sha2_password`. Go through `dongjoome-mysql` directly.

## Evidence, retained deliberately

| What | Where |
|---|---|
| Compromised datadir | `/mnt/hdd/data/dongjoo.me/db` on app01, container `dongjoome-db-1` stopped with `--restart=no` |
| Pre-rebuild DB dump | `/mnt/hdd/rebuild/dongjoo-20260929/dongduwp-evidence-20260929.sql.gz` |
| Original document root | the held ZFS snapshot above |

⚠️ Two ways to disturb the retired container, both verified against the
deployed Compose 5.5.1:

- `docker compose down --remove-orphans` in this directory **deletes** it. The
  datadir survives, but the container and its metadata do not.
- `docker compose -p dongjoome start` (or `restart`) reconstructs the project
  from existing container labels, which still carry `service=db`, and starts
  the compromised instance. `--restart=no` does not prevent an explicit start.

  **Being in this directory does not help.** `-p` bypasses file discovery
  wherever it is run from. Measured here with `--dry-run`:

  | command, from this directory | targets |
  |---|---|
  | `docker compose -p dongjoome start` | `dongjoome-db-1 Starting` ← the retired container |
  | `docker compose start` | reads this file; `db` is not in it |
  | `docker compose -f docker-compose.yml -p dongjoome start` | reads this file; `db` is not in it |

  So: **either omit `-p` and let directory discovery load this file, or pass
  `-f docker-compose.yml` explicitly.** Never `-p` alone.

## Known outstanding

**Cloudflare 301-redirects every `dongjoo.me` path to the identical HTTPS URL
without ever reaching the origin.** The origin is verified good — over the
tailnet, `https://dongjoo.me/` returns 200 with the real certificate, all 14
posts and pages render, `/comments/` reaches remark42. This is a zone-level
setting (SSL/TLS mode, "Always Use HTTPS", or a Redirect Rule) and the
DNS-scoped token on edge01 cannot read it, so **the site stays publicly
unreachable until that is changed in the Cloudflare dashboard**.

remark42's Google and Facebook OAuth client secrets were in the old `.env` and
are carried forward unchanged — they were reachable by the webshell and should
be rotated in their respective consoles.
