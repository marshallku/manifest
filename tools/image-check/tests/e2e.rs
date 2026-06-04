//! End-to-end test: run the real binary against the real manifest repo, hitting
//! live registries. Gated with `#[ignore]` so the default `cargo test` stays
//! hermetic/offline; run explicitly with `cargo test -- --ignored`.

use std::path::PathBuf;
use std::process::Command;

use serde_json::Value;

fn repo_root() -> PathBuf {
    // tools/image-check/ -> ../../ is the repo root.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .expect("repo root")
}

#[test]
#[ignore = "hits live registries; run with --ignored"]
fn checks_real_repo_against_live_registries() {
    let bin = env!("CARGO_BIN_EXE_image-check");
    let root = repo_root();

    let out = Command::new(bin)
        .arg(&root)
        .arg("--json")
        .output()
        .expect("run image-check");

    let code = out.status.code().unwrap_or(-1);
    // 0 (all current), 10 (some outdated), or 20 (some errors) are all valid
    // runs. 1 (no images) / 2 (usage) mean the tool is broken.
    assert!(
        matches!(code, 0 | 10 | 20),
        "unexpected exit code {code}; stderr:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );

    let json: Value = serde_json::from_slice(&out.stdout).expect("stdout is JSON");
    let arr = json.as_array().expect("top-level array");
    assert!(
        arr.len() >= 20,
        "expected the repo's ~27 third-party images, got {}",
        arr.len()
    );

    // Every entry must carry a non-empty current tag and a registry endpoint.
    for e in arr {
        assert!(
            !e["current"].as_str().unwrap_or("").is_empty(),
            "empty current in {e}"
        );
        assert!(
            !e["endpoint"].as_str().unwrap_or("").is_empty(),
            "empty endpoint in {e}"
        );
    }

    // Registries should be broadly reachable: tolerate the odd transient error
    // but not a wholesale failure.
    let errors = arr.iter().filter(|e| !e["error"].is_null()).count();
    assert!(
        errors <= 3,
        "too many registry errors ({errors}/{}); checker likely broken",
        arr.len()
    );

    // argocd must be discovered (it lives in kubernetes/argocd as a quay.io ref).
    let argocd = arr
        .iter()
        .find(|e| e["repository"].as_str() == Some("argoproj/argocd"))
        .expect("argocd should be among the checked images");
    assert!(
        !argocd["latest"].as_str().unwrap_or("").is_empty(),
        "argocd should resolve a latest tag: {argocd}"
    );

    // The whole point of the tool: at least one image should be detectable as
    // outdated across ~27 third-party images.
    let outdated = arr
        .iter()
        .filter(|e| e["outdated"] == Value::Bool(true))
        .count();
    assert!(
        outdated >= 1,
        "expected at least one outdated image among {}",
        arr.len()
    );
}
