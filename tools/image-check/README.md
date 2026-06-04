# image-check

Flags **outdated third-party container images** across this GitOps repo.

It scans every `image:` reference under `kubernetes/**` and `docker-compose/**`,
ignores first-party CI-deployed images (git-SHA / `latest` tags), and compares
each remaining tag against the tags actually published in its registry.

## Why a custom tool

The images here use wildly different tag schemes — `v0.107.71`, CalVer
`2025.4.2`, `2.7.11-alpine`, `31.0.13-apache`, `16-alpine` vs `18.1-alpine` vs
`18.3-alpine3.23`, `6.9.0-php8.5-apache`, `7-alpine`. Generic "is there a
`:latest`?" checks are useless here, and it's easy to miss a new **major**
version hiding behind an unusual tag.

### How comparison works (`src/version.rs`)

Each tag is tokenized into a **template** (every digit run replaced by `#`) plus
its numeric values:

| tag | template | numbers |
|---|---|---|
| `v0.107.71` | `v#.#.#` | `[0,107,71]` |
| `2025.4.2` | `#.#.#` | `[2025,4,2]` |
| `16-alpine` | `#-alpine` | `[16]` |
| `18.3-alpine3.23` | `#.#-alpine#.#` | `[18,3,3,23]` |
| `6.9.0-php8.5-apache` | `#.#.#-php#.#-apache` | `[6,9,0,8,5]` |

Two tags are **comparable only if their templates match**. That keeps `-alpine`
apart from `-apache`, a major-only `16-alpine` apart from `18.1-alpine`, and a
`v`-prefixed scheme apart from a bare one. Among comparable tags the newest wins
by numeric ordering; the first numeric field changing => `major`. Pre-release
channels (`-rc`, `-beta`, …) are skipped unless the current tag is itself a
pre-release.

This deliberately prefers **false-negatives (stay quiet) over false-positives
(noise)** — a base-flavor change such as `alpine3.23 -> alpine` is treated as a
different lineage rather than mis-reported as an update.

## Usage

```bash
# from the repo root
cargo run --release --manifest-path tools/image-check/Cargo.toml -- .

# JSON (for scripts / CI)
... -- . --json

# GitHub-issue-ready Markdown
... -- . --markdown

# narrow down while iterating
... -- . --only argocd
```

Flags: `--json`, `--markdown`, `--only <substr>`, `--ignore <prefix>`
(repeatable; adds to the first-party defaults), `--jobs <N>` (registry query
concurrency, default 8).

Exit codes: `0` all current · `10` some outdated · `20` registry errors and
nothing outdated · `1` no images found · `2` bad usage. The non-zero "outdated"
code is what the workflow keys off — handle it explicitly when scripting.

## Tests

```bash
cargo test                 # offline unit tests (parsing / compare / render)
cargo test -- --ignored    # e2e: runs the binary against this repo + live registries
```

## Automation

`.github/workflows/image-updates.yml` runs this weekly (Mon 08:00 KST) and
upserts a single tracking Issue labelled `image-updates` when anything is
behind, auto-closing it once everything is current.
