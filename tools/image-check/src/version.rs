//! Quirky-tag version logic.
//!
//! Container tags in this repo use wildly different schemes: `v0.107.71`,
//! CalVer `2025.4.2`, `2.7.11-alpine`, `31.0.13-apache`, `16-alpine` vs
//! `18.1-alpine` vs `18.3-alpine3.23`, `6.9.0-php8.5-apache`, `7-alpine`...
//!
//! The trick: tokenize a tag into a **template** (every maximal digit run
//! replaced by `#`) plus the ordered list of numeric values. Two tags are
//! *comparable* iff their templates are identical — that isolates `-alpine`
//! from `-apache`, a major-only `16-alpine` (one number) from `18.1-alpine`
//! (two numbers), and a `v`-prefixed scheme from a bare one. Among comparable
//! tags we pick the max by numeric-tuple ordering. This deliberately prefers
//! false-negatives (stay silent) over false-positives (noise) for a homelab
//! notifier — a base-image flavor change (`alpine3.23` -> `alpine`) is treated
//! as a different lineage and ignored rather than mis-reported.

use std::fmt;

/// Markers that indicate a pre-release / non-stable channel. Checked against
/// the *template* (digits already collapsed to `#`). Stable repo tags such as
/// `#.#.#-alpine`, `#.#.#-apache`, `#.#.#-php#.#-apache` contain none of these.
const PRERELEASE_MARKERS: &[&str] = &[
    "rc", "beta", "alpha", "nightly", "canary", "snapshot", "unstable", "preview", "edge", "-dev",
    "-test", "-git", "insider",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bump {
    Major,
    Minor,
    Patch,
    None,
}

impl fmt::Display for Bump {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            Bump::Major => "major",
            Bump::Minor => "minor",
            Bump::Patch => "patch",
            Bump::None => "none",
        };
        f.write_str(s)
    }
}

/// A tag parsed into its comparable shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parsed {
    pub raw: String,
    /// Digit runs collapsed to `#`, e.g. `v#.#.#`, `#.#.#-php#.#-apache`.
    pub template: String,
    /// Numeric values in order of appearance.
    pub nums: Vec<u64>,
}

/// Tokenize a tag. Returns `None` when the tag carries no numeric component
/// (`latest`, `master`, `stable`, ...), which makes it unrankable.
pub fn parse_tag(tag: &str) -> Option<Parsed> {
    let mut template = String::with_capacity(tag.len());
    let mut nums = Vec::new();
    let mut chars = tag.chars().peekable();

    while let Some(&c) = chars.peek() {
        if c.is_ascii_digit() {
            let mut n: u64 = 0;
            let mut overflowed = false;
            while let Some(&d) = chars.peek() {
                if !d.is_ascii_digit() {
                    break;
                }
                chars.next();
                n = match n
                    .checked_mul(10)
                    .and_then(|v| v.checked_add((d as u8 - b'0') as u64))
                {
                    Some(v) => v,
                    None => {
                        overflowed = true;
                        u64::MAX
                    }
                };
            }
            // A run that overflows u64 is not a sane version field (likely a
            // hex/sha-ish blob); treat the whole tag as unrankable.
            if overflowed {
                return None;
            }
            template.push('#');
            nums.push(n);
        } else {
            template.push(c.to_ascii_lowercase());
            chars.next();
        }
    }

    if nums.is_empty() {
        return None;
    }
    Some(Parsed {
        raw: tag.to_string(),
        template,
        nums,
    })
}

fn is_prerelease(template: &str) -> bool {
    PRERELEASE_MARKERS.iter().any(|m| template.contains(m))
}

/// Classify the jump from `current` to `candidate` (assumed same template,
/// hence same length).
pub fn bump_class(current: &[u64], candidate: &[u64]) -> Bump {
    for (i, (a, b)) in current.iter().zip(candidate.iter()).enumerate() {
        if a != b {
            return match i {
                0 => Bump::Major,
                1 => Bump::Minor,
                _ => Bump::Patch,
            };
        }
    }
    Bump::None
}

/// The outcome of comparing a current tag against a registry's tag list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Comparison {
    pub current: String,
    pub latest: String,
    pub bump: Bump,
    pub outdated: bool,
}

/// Given the deployed `current` tag and all `available` registry tags, find the
/// newest comparable tag. Returns `None` when the current tag is itself
/// unrankable (e.g. `latest`) — those should be filtered out before calling.
pub fn evaluate(current: &str, available: &[String]) -> Option<Comparison> {
    let cur = parse_tag(current)?;
    let cur_is_pre = is_prerelease(&cur.template);

    let mut best: Option<Parsed> = None;
    for tag in available {
        let Some(p) = parse_tag(tag) else { continue };
        if p.template != cur.template {
            continue;
        }
        if is_prerelease(&p.template) && !cur_is_pre {
            continue;
        }
        match &best {
            Some(b) if p.nums <= b.nums => {}
            _ => best = Some(p),
        }
    }

    // If the registry never returned a tag matching our template (not even the
    // current one), we cannot make a safe judgement — report current as latest.
    let best = best.unwrap_or_else(|| cur.clone());
    let bump = bump_class(&cur.nums, &best.nums);
    let outdated = best.nums > cur.nums;
    Some(Comparison {
        current: current.to_string(),
        latest: best.raw,
        bump,
        outdated,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpl(tag: &str) -> String {
        parse_tag(tag).unwrap().template
    }
    fn nums(tag: &str) -> Vec<u64> {
        parse_tag(tag).unwrap().nums
    }

    #[test]
    fn templates_cover_every_repo_scheme() {
        assert_eq!(tmpl("v0.107.71"), "v#.#.#");
        assert_eq!(nums("v0.107.71"), vec![0, 107, 71]);
        assert_eq!(tmpl("2025.4.2"), "#.#.#");
        assert_eq!(tmpl("2.7.11-alpine"), "#.#.#-alpine");
        assert_eq!(tmpl("31.0.13-apache"), "#.#.#-apache");
        assert_eq!(tmpl("16-alpine"), "#-alpine");
        assert_eq!(tmpl("18.1-alpine"), "#.#-alpine");
        assert_eq!(tmpl("18.3-alpine3.23"), "#.#-alpine#.#");
        assert_eq!(nums("18.3-alpine3.23"), vec![18, 3, 3, 23]);
        assert_eq!(tmpl("6.9.0-php8.5-apache"), "#.#.#-php#.#-apache");
        assert_eq!(nums("6.9.0-php8.5-apache"), vec![6, 9, 0, 8, 5]);
        assert_eq!(tmpl("7-alpine"), "#-alpine");
        assert_eq!(tmpl("1.37"), "#.#");
    }

    #[test]
    fn non_numeric_tags_are_unrankable() {
        assert!(parse_tag("latest").is_none());
        assert!(parse_tag("master").is_none());
        assert!(parse_tag("stable").is_none());
        // a git sha is non-numeric-leading mixed; has digits though -> still parses,
        // but callers filter SHAs out by length before reaching here.
        assert!(parse_tag("prd-placeholder").is_none());
    }

    #[test]
    fn different_flavors_are_not_comparable() {
        // alpine current must not be "upgraded" to an apache tag and vice versa
        let avail = vec![
            "16-alpine".to_string(),
            "17-alpine".to_string(),
            "18-alpine".to_string(),
            "18".to_string(),          // different template (#)
            "18.1-alpine".to_string(), // different template (#.#-alpine)
        ];
        let c = evaluate("16-alpine", &avail).unwrap();
        assert_eq!(c.latest, "18-alpine");
        assert!(c.outdated);
        assert_eq!(c.bump, Bump::Major);
    }

    #[test]
    fn calver_orders_naturally() {
        let avail = vec![
            "2025.4.2".to_string(),
            "2025.5.0".to_string(),
            "2024.12.1".to_string(),
            "2026.1.0".to_string(),
        ];
        let c = evaluate("2025.4.2", &avail).unwrap();
        assert_eq!(c.latest, "2026.1.0");
        assert!(c.outdated);
        assert_eq!(c.bump, Bump::Major); // first field changed
    }

    #[test]
    fn v_prefixed_minor_bump() {
        let avail = vec![
            "v3.3.5".to_string(),
            "v3.4.3".to_string(),
            "v3.3.11".to_string(),
            "v3.4.3-rc1".to_string(), // prerelease, different template -> ignored
        ];
        let c = evaluate("v3.3.5", &avail).unwrap();
        assert_eq!(c.latest, "v3.4.3");
        assert_eq!(c.bump, Bump::Minor);
        assert!(c.outdated);
    }

    #[test]
    fn prereleases_are_skipped_for_stable_current() {
        let avail = vec![
            "1.2.3".to_string(),
            "1.3.0-rc1".to_string(),
            "1.2.4-beta".to_string(),
        ];
        let c = evaluate("1.2.3", &avail).unwrap();
        assert_eq!(c.latest, "1.2.3");
        assert!(!c.outdated);
    }

    #[test]
    fn up_to_date_reports_not_outdated() {
        let avail = vec![
            "12.3.1".to_string(),
            "12.3.2".to_string(),
            "12.3.0".to_string(),
        ];
        let c = evaluate("12.3.2", &avail).unwrap();
        assert_eq!(c.latest, "12.3.2");
        assert!(!c.outdated);
        assert_eq!(c.bump, Bump::None);
    }

    #[test]
    fn multi_component_suffix_php_apache() {
        let avail = vec![
            "6.9.0-php8.5-apache".to_string(),
            "6.9.1-php8.5-apache".to_string(),
            "6.9.0-php8.4-apache".to_string(),
            "6.9.0-php8.5-fpm".to_string(), // different flavor
        ];
        let c = evaluate("6.9.0-php8.5-apache", &avail).unwrap();
        assert_eq!(c.latest, "6.9.1-php8.5-apache");
        assert!(c.outdated);
        assert_eq!(c.bump, Bump::Patch); // wordpress patch field
    }

    #[test]
    fn empty_or_unmatched_registry_is_safe() {
        let c = evaluate("1.2.3", &[]).unwrap();
        assert_eq!(c.latest, "1.2.3");
        assert!(!c.outdated);
        let c2 = evaluate("1.2.3", &["latest".to_string(), "stable".to_string()]).unwrap();
        assert!(!c2.outdated);
    }
}
