//! Find `image:` references in the repo's YAML manifests and decide which ones
//! are worth version-checking (third-party, tag-pinned, not git-SHA / `latest`).

use std::fs;
use std::path::Path;
use std::sync::OnceLock;

use regex::Regex;

/// Prefixes whose images are first-party and CI-deployed (git-SHA / `latest`
/// tags) — never version-checked. Extendable via `--ignore` on the CLI.
pub const DEFAULT_IGNORE_PREFIXES: &[&str] = &["ghcr.io/80rian/", "ghcr.io/marshallku/"];

fn image_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    // `image:` as a YAML key (start of line or after a `- `), value optionally
    // quoted. Leading-`#` (commented) lines are rejected by the caller.
    RE.get_or_init(|| Regex::new(r#"(?m)^\s*(?:-\s+)?image:\s*["']?([^"'#\s]+)["']?\s*$"#).unwrap())
}

/// Extract image refs from a single file's text. Skips commented lines.
pub fn extract_images_from_str(content: &str) -> Vec<String> {
    let re = image_re();
    let mut out = Vec::new();
    for line in content.lines() {
        if line.trim_start().starts_with('#') {
            continue;
        }
        if let Some(c) = re.captures(line) {
            out.push(c[1].to_string());
        }
    }
    out
}

/// A 7-40 char lowercase hex string — a git short/long SHA used by first-party
/// CI tags. Defensive: third-party images are not SHA-tagged.
fn looks_like_git_sha(tag: &str) -> bool {
    let len = tag.len();
    (7..=40).contains(&len)
        && tag.chars().all(|c| c.is_ascii_hexdigit())
        && tag.chars().any(|c| c.is_ascii_digit())
}

/// Decide whether a ref should be version-checked.
pub fn is_checkable(image_ref: &str, ignore_prefixes: &[String]) -> bool {
    if ignore_prefixes
        .iter()
        .any(|p| image_ref.starts_with(p.as_str()))
    {
        return false;
    }
    let r = crate::reference::parse(image_ref);
    match r.tag.as_deref() {
        None => false, // digest-pinned or untagged
        Some("latest") => false,
        Some(tag) if looks_like_git_sha(tag) => false,
        Some(tag) => crate::version::parse_tag(tag).is_some(), // must be rankable
    }
}

/// Recursively collect `.yaml`/`.yml` files under `root`.
fn yaml_files(root: &Path, acc: &mut Vec<std::path::PathBuf>) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if path.is_dir() {
            if name == ".git" || name == "target" || name == "node_modules" {
                continue;
            }
            yaml_files(&path, acc);
        } else if matches!(
            path.extension().and_then(|e| e.to_str()),
            Some("yaml") | Some("yml")
        ) {
            acc.push(path);
        }
    }
}

/// Scan `root`, returning the deduped, sorted set of checkable third-party refs.
pub fn scan(root: &Path, ignore_prefixes: &[String]) -> Vec<String> {
    let mut files = Vec::new();
    yaml_files(root, &mut files);

    let mut refs: Vec<String> = Vec::new();
    for file in files {
        let Ok(content) = fs::read_to_string(&file) else {
            continue;
        };
        for image in extract_images_from_str(&content) {
            if is_checkable(&image, ignore_prefixes) && !refs.contains(&image) {
                refs.push(image);
            }
        }
    }
    refs.sort();
    refs
}

#[cfg(test)]
mod tests {
    use super::*;

    fn defaults() -> Vec<String> {
        DEFAULT_IGNORE_PREFIXES
            .iter()
            .map(|s| s.to_string())
            .collect()
    }

    #[test]
    fn extracts_quoted_and_unquoted_and_list_items() {
        let yaml = r#"
services:
  db:
    image: postgres:16-alpine
  cache:
    image: "redis:7-alpine"
spec:
  containers:
    - image: quay.io/argoproj/argocd:v3.3.5
    # image: should/not:match
      name: x
"#;
        let imgs = extract_images_from_str(yaml);
        assert_eq!(
            imgs,
            vec![
                "postgres:16-alpine",
                "redis:7-alpine",
                "quay.io/argoproj/argocd:v3.3.5",
            ]
        );
    }

    #[test]
    fn first_party_and_latest_and_sha_are_skipped() {
        assert!(!is_checkable(
            "ghcr.io/80rian/maji-web:2faf05b1cf2738296622f38bbebcc136f78c1bd7",
            &defaults()
        ));
        assert!(!is_checkable(
            "ghcr.io/marshallku/blog-backend:latest",
            &defaults()
        ));
        assert!(!is_checkable(
            "ghcr.io/marshallku/traffic-switcher:22",
            &defaults()
        )); // ignored by prefix
        assert!(!is_checkable(
            "ghcr.io/80rian/irang-web:prd-placeholder",
            &defaults()
        ));
        // bare sha tag on some hypothetical third-party
        assert!(!is_checkable(
            "example.com/foo/bar:7640a62bcb7977e8e801e11fd6abc3d932ee1dab",
            &defaults()
        ));
    }

    #[test]
    fn third_party_pinned_tags_are_checkable() {
        for img in [
            "postgres:16-alpine",
            "quay.io/argoproj/argocd:v3.3.5",
            "grafana/grafana:12.3.2",
            "wordpress:6.9.0-php8.5-apache",
            "cloudflare/cloudflared:2025.4.2",
            "public.ecr.aws/docker/library/redis:8.2.3-alpine",
        ] {
            assert!(is_checkable(img, &defaults()), "{img} should be checkable");
        }
    }

    #[test]
    fn sha_heuristic() {
        assert!(looks_like_git_sha(
            "2faf05b1cf2738296622f38bbebcc136f78c1bd7"
        ));
        assert!(looks_like_git_sha("7640a62"));
        assert!(!looks_like_git_sha("16")); // too short / version
        assert!(!looks_like_git_sha("v3.3.5"));
        assert!(!looks_like_git_sha("alpine")); // no digit
    }
}
