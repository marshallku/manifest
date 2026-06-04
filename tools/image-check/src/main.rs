//! image-check — flag outdated third-party container images in this GitOps repo.
//!
//! Scans `kubernetes/**` and `docker-compose/**` for `image:` refs, skips
//! first-party CI-deployed images, and compares each remaining tag against its
//! registry's available tags using template-aware version logic (see
//! `version.rs`). Prints a table (or `--json`) and signals via exit code.
//!
//! Exit codes: 0 = all current · 10 = some outdated · 20 = registry errors and
//! nothing outdated · 1 = no images found · 2 = bad usage.

mod reference;
mod registry;
mod scan;
mod version;

use std::io::IsTerminal;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::thread;

use version::Bump;

struct Args {
    root: PathBuf,
    json: bool,
    markdown: bool,
    only: Option<String>,
    jobs: usize,
    ignore: Vec<String>,
}

struct Report {
    image: String,
    endpoint: String,
    repository: String,
    current: String,
    latest: String,
    bump: Bump,
    outdated: bool,
    error: Option<String>,
}

const USAGE: &str = "\
image-check — flag outdated third-party container images

USAGE:
    image-check [ROOT] [OPTIONS]

ARGS:
    ROOT                 Repo root to scan (default: current directory)

OPTIONS:
    --json               Emit JSON instead of a table
    --markdown           Emit a GitHub-issue-ready Markdown report
    --only <SUBSTR>      Only check images whose ref contains SUBSTR
    --ignore <PREFIX>    Skip refs starting with PREFIX (repeatable; adds to defaults)
    --jobs <N>           Concurrent registry queries (default: 8)
    -h, --help           Show this help
";

fn parse_args() -> Result<Args, String> {
    let mut root: Option<PathBuf> = None;
    let mut json = false;
    let mut markdown = false;
    let mut only = None;
    let mut jobs = 8usize;
    let mut ignore: Vec<String> = scan::DEFAULT_IGNORE_PREFIXES
        .iter()
        .map(|s| s.to_string())
        .collect();

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            "--json" => json = true,
            "--markdown" => markdown = true,
            "--only" => only = Some(it.next().ok_or("--only needs a value")?),
            "--ignore" => ignore.push(it.next().ok_or("--ignore needs a value")?),
            "--jobs" => {
                jobs = it
                    .next()
                    .ok_or("--jobs needs a value")?
                    .parse()
                    .map_err(|_| "--jobs must be a number")?;
                jobs = jobs.max(1);
            }
            s if s.starts_with('-') => return Err(format!("unknown flag: {s}")),
            s => {
                if root.is_some() {
                    return Err(format!("unexpected argument: {s}"));
                }
                root = Some(PathBuf::from(s));
            }
        }
    }

    Ok(Args {
        root: root.unwrap_or_else(|| PathBuf::from(".")),
        json,
        markdown,
        only,
        jobs,
        ignore,
    })
}

fn process(client: &registry::Client, image: &str) -> Report {
    let r = reference::parse(image);
    let current = r.tag.clone().unwrap_or_default();
    let mut report = Report {
        image: image.to_string(),
        endpoint: r.endpoint.clone(),
        repository: r.repository.clone(),
        current: current.clone(),
        latest: current.clone(),
        bump: Bump::None,
        outdated: false,
        error: None,
    };

    match client.list_tags(&r.endpoint, &r.repository) {
        Ok(tags) => match version::evaluate(&current, &tags) {
            Some(c) => {
                report.latest = c.latest;
                report.bump = c.bump;
                report.outdated = c.outdated;
            }
            None => report.error = Some("current tag is unrankable".to_string()),
        },
        Err(e) => report.error = Some(e.to_string()),
    }
    report
}

fn bump_rank(b: Bump) -> u8 {
    match b {
        Bump::Major => 0,
        Bump::Minor => 1,
        Bump::Patch => 2,
        Bump::None => 3,
    }
}

fn category(r: &Report) -> u8 {
    if r.outdated {
        0
    } else if r.error.is_some() {
        1
    } else {
        2
    }
}

fn render_table(reports: &[Report]) {
    let color = std::io::stdout().is_terminal();
    let (red, yellow, green, dim, reset) = if color {
        ("\x1b[31m", "\x1b[33m", "\x1b[32m", "\x1b[2m", "\x1b[0m")
    } else {
        ("", "", "", "", "")
    };

    let img_w = reports
        .iter()
        .map(|r| r.image.len())
        .max()
        .unwrap_or(5)
        .max(5);
    let cur_w = reports
        .iter()
        .map(|r| r.current.len())
        .max()
        .unwrap_or(7)
        .max(7);
    let lat_w = reports
        .iter()
        .map(|r| r.latest.len())
        .max()
        .unwrap_or(6)
        .max(6);

    println!(
        "{dim}{:<8} {:<img_w$} {:<cur_w$} {:<lat_w$} BUMP{reset}",
        "STATUS", "IMAGE", "CURRENT", "LATEST"
    );
    for r in reports {
        let (status, scol) = if r.error.is_some() {
            ("ERR", red)
        } else if r.outdated {
            match r.bump {
                Bump::Major => ("OUTDATED", red),
                _ => ("OUTDATED", yellow),
            }
        } else {
            ("ok", green)
        };
        let bump = match &r.error {
            Some(e) => e.clone(),
            None if r.outdated => match r.bump {
                Bump::Major => "MAJOR".to_string(),
                other => other.to_string(),
            },
            None => "-".to_string(),
        };
        println!(
            "{scol}{:<8}{reset} {:<img_w$} {:<cur_w$} {scol}{:<lat_w$}{reset} {scol}{}{reset}",
            status, r.image, r.current, r.latest, bump
        );
    }

    let outdated = reports.iter().filter(|r| r.outdated).count();
    let majors = reports
        .iter()
        .filter(|r| r.outdated && r.bump == Bump::Major)
        .count();
    let errors = reports.iter().filter(|r| r.error.is_some()).count();
    println!(
        "\n{} images checked · {} outdated ({} major) · {} errors",
        reports.len(),
        outdated,
        majors,
        errors
    );
}

/// The image ref without its `:tag` suffix, for compact display.
fn name_only(r: &Report) -> &str {
    r.image
        .strip_suffix(&format!(":{}", r.current))
        .unwrap_or(&r.image)
}

/// A GitHub-issue-ready Markdown report. Deterministic (no timestamp) so the
/// issue body only changes when the set of updates changes.
fn render_markdown(reports: &[Report]) -> String {
    let outdated: Vec<&Report> = reports.iter().filter(|r| r.outdated).collect();
    let majors: Vec<&&Report> = outdated.iter().filter(|r| r.bump == Bump::Major).collect();
    let errors: Vec<&Report> = reports.iter().filter(|r| r.error.is_some()).collect();

    let mut s = String::new();
    s.push_str("## 🔔 Outdated third-party images\n\n");

    if outdated.is_empty() {
        s.push_str("All third-party images are up to date. ✅\n");
    } else {
        s.push_str(&format!(
            "**{} of {} checked images are outdated** ({} major bump{}).\n\n",
            outdated.len(),
            reports.len(),
            majors.len(),
            if majors.len() == 1 { "" } else { "s" }
        ));

        if !majors.is_empty() {
            s.push_str("### ⬆️ Major version bumps\n\n");
            s.push_str("| Image | Current | Latest |\n|---|---|---|\n");
            for r in &majors {
                s.push_str(&format!(
                    "| `{}` | `{}` | `{}` |\n",
                    name_only(r),
                    r.current,
                    r.latest
                ));
            }
            s.push('\n');
        }

        let minors: Vec<&&Report> = outdated.iter().filter(|r| r.bump != Bump::Major).collect();
        if !minors.is_empty() {
            s.push_str("### Minor / patch\n\n");
            s.push_str("| Image | Current | Latest | Bump |\n|---|---|---|---|\n");
            for r in &minors {
                s.push_str(&format!(
                    "| `{}` | `{}` | `{}` | {} |\n",
                    name_only(r),
                    r.current,
                    r.latest,
                    r.bump
                ));
            }
            s.push('\n');
        }
    }

    if !errors.is_empty() {
        s.push_str("### ⚠️ Could not check\n\n");
        for r in &errors {
            s.push_str(&format!(
                "- `{}` — {}\n",
                name_only(r),
                r.error.as_deref().unwrap_or("unknown error")
            ));
        }
        s.push('\n');
    }

    s.push_str("\n<sub>Generated by `tools/image-check` · regenerated weekly</sub>\n");
    s
}

fn render_json(reports: &[Report]) {
    let arr: Vec<serde_json::Value> = reports
        .iter()
        .map(|r| {
            serde_json::json!({
                "image": r.image,
                "endpoint": r.endpoint,
                "repository": r.repository,
                "current": r.current,
                "latest": r.latest,
                "bump": r.bump.to_string(),
                "outdated": r.outdated,
                "error": r.error,
            })
        })
        .collect();
    println!("{}", serde_json::to_string_pretty(&arr).unwrap());
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    let mut refs = scan::scan(&args.root, &args.ignore);
    if let Some(sub) = &args.only {
        refs.retain(|r| r.contains(sub.as_str()));
    }
    if refs.is_empty() {
        eprintln!(
            "no checkable third-party images found under {}",
            args.root.display()
        );
        return ExitCode::from(1);
    }

    let client = registry::Client::new();
    let results: Mutex<Vec<Report>> = Mutex::new(Vec::with_capacity(refs.len()));
    let next = AtomicUsize::new(0);
    let jobs = args.jobs.min(refs.len());

    thread::scope(|s| {
        for _ in 0..jobs {
            s.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= refs.len() {
                    break;
                }
                let report = process(&client, &refs[i]);
                results.lock().unwrap().push(report);
            });
        }
    });

    let mut reports = results.into_inner().unwrap();
    reports.sort_by(|a, b| {
        category(a)
            .cmp(&category(b))
            .then(bump_rank(a.bump).cmp(&bump_rank(b.bump)))
            .then(a.image.cmp(&b.image))
    });

    if args.json {
        render_json(&reports);
    } else if args.markdown {
        print!("{}", render_markdown(&reports));
    } else {
        render_table(&reports);
    }

    let any_outdated = reports.iter().any(|r| r.outdated);
    let any_error = reports.iter().any(|r| r.error.is_some());
    if any_outdated {
        ExitCode::from(10)
    } else if any_error {
        ExitCode::from(20)
    } else {
        ExitCode::SUCCESS
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(image: &str, current: &str, latest: &str, bump: Bump, outdated: bool) -> Report {
        Report {
            image: image.to_string(),
            endpoint: "registry-1.docker.io".to_string(),
            repository: "library/x".to_string(),
            current: current.to_string(),
            latest: latest.to_string(),
            bump,
            outdated,
            error: None,
        }
    }

    #[test]
    fn name_only_strips_tag() {
        let r = report(
            "quay.io/argoproj/argocd:v3.3.5",
            "v3.3.5",
            "v3.4.3",
            Bump::Minor,
            true,
        );
        assert_eq!(name_only(&r), "quay.io/argoproj/argocd");
    }

    #[test]
    fn markdown_groups_major_and_minor() {
        let reports = vec![
            report(
                "postgres:16-alpine",
                "16-alpine",
                "18-alpine",
                Bump::Major,
                true,
            ),
            report("redis:7-alpine", "7-alpine", "8-alpine", Bump::Major, true),
            report("mariadb:12.1.2", "12.1.2", "12.3.2", Bump::Minor, true),
            report("cadvisor:v0.55.1", "v0.55.1", "v0.55.1", Bump::None, false),
        ];
        let md = render_markdown(&reports);
        assert!(md.contains("3 of 4 checked images are outdated"));
        assert!(md.contains("(2 major bumps)"));
        assert!(md.contains("### ⬆️ Major version bumps"));
        assert!(md.contains("| `postgres` | `16-alpine` | `18-alpine` |"));
        assert!(md.contains("### Minor / patch"));
        assert!(md.contains("| `mariadb` | `12.1.2` | `12.3.2` | minor |"));
        // up-to-date image is not listed
        assert!(!md.contains("cadvisor"));
    }

    #[test]
    fn markdown_all_current() {
        let reports = vec![report("x:1.0.0", "1.0.0", "1.0.0", Bump::None, false)];
        let md = render_markdown(&reports);
        assert!(md.contains("All third-party images are up to date"));
    }
}
