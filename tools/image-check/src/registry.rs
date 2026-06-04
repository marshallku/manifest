//! Generic Docker Registry HTTP API v2 client.
//!
//! One code path serves every registry in the repo (Docker Hub, ghcr, quay,
//! gcr, public.ecr.aws, docker.n8n.io): hit `/v2/<repo>/tags/list`, and on a
//! `401` read the `WWW-Authenticate: Bearer realm/service/scope` challenge,
//! fetch a token from `realm`, and retry. The challenge's own `scope` is echoed
//! verbatim when present — that is what makes `public.ecr.aws` (which demands
//! `scope=aws`, not `repository:...`) work without special-casing. Tag lists are
//! paginated via the `Link: ...; rel="next"` header.

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use serde_json::Value;

pub struct Client {
    agent: ureq::Agent,
}

impl Client {
    pub fn new() -> Self {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(10))
            .timeout_read(Duration::from_secs(30))
            .user_agent("manifest-image-check/0.1 (+homelab)")
            .build();
        Self { agent }
    }

    /// List every tag for `repo` at `endpoint`. Network/auth failures surface as
    /// `Err` so the caller can report the image as errored without aborting the
    /// whole run.
    pub fn list_tags(&self, endpoint: &str, repo: &str) -> Result<Vec<String>> {
        let mut token: Option<String> = None;
        let mut url = format!("https://{endpoint}/v2/{repo}/tags/list?n=1000");
        let mut tags = Vec::new();

        for _ in 0..50 {
            let resp = self.get_with_auth(&url, &mut token, repo)?;
            let link = resp.header("link").map(str::to_string);
            let body = resp.into_string().context("reading tags response")?;
            let json: Value = serde_json::from_str(&body)
                .with_context(|| format!("parsing tags json from {url}"))?;
            if let Some(arr) = json.get("tags").and_then(Value::as_array) {
                tags.extend(arr.iter().filter_map(Value::as_str).map(str::to_string));
            }
            match next_link(link.as_deref(), endpoint) {
                Some(next) => url = next,
                None => return Ok(tags),
            }
        }
        Ok(tags) // pagination guard tripped; return what we have
    }

    fn get_with_auth(
        &self,
        url: &str,
        token: &mut Option<String>,
        repo: &str,
    ) -> Result<ureq::Response> {
        let build = |tok: Option<&str>| {
            let mut req = self.agent.get(url).set("Accept", "application/json");
            if let Some(t) = tok {
                req = req.set("Authorization", &format!("Bearer {t}"));
            }
            req
        };

        match build(token.as_deref()).call() {
            Ok(r) => Ok(r),
            Err(ureq::Error::Status(401, r)) => {
                let challenge = r
                    .header("www-authenticate")
                    .ok_or_else(|| anyhow!("401 with no WWW-Authenticate header"))?
                    .to_string();
                let fresh = self.fetch_token(&challenge, repo)?;
                let resp = build(Some(&fresh))
                    .call()
                    .map_err(|e| anyhow!("retry after auth failed: {e}"))?;
                *token = Some(fresh);
                Ok(resp)
            }
            Err(ureq::Error::Status(code, _)) => Err(anyhow!("registry returned HTTP {code}")),
            Err(e) => Err(anyhow!("request failed: {e}")),
        }
    }

    fn fetch_token(&self, challenge: &str, repo: &str) -> Result<String> {
        let realm = kv(challenge, "realm").ok_or_else(|| anyhow!("no realm in challenge"))?;
        let service = kv(challenge, "service");
        // Echo the server's scope verbatim (handles ecr `scope=aws`); fall back
        // to the conventional pull scope.
        let scope = kv(challenge, "scope").unwrap_or_else(|| format!("repository:{repo}:pull"));

        // Build the token URL by hand — registries accept the raw `:`/`/` in the
        // scope, and hand-encoding risks mismatches.
        let url = match service {
            Some(svc) => format!("{realm}?service={svc}&scope={scope}"),
            None => format!("{realm}?scope={scope}"),
        };
        let body = self
            .agent
            .get(&url)
            .call()
            .map_err(|e| anyhow!("token request failed: {e}"))?
            .into_string()?;
        let json: Value = serde_json::from_str(&body).context("parsing token json")?;
        json.get("token")
            .or_else(|| json.get("access_token"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| anyhow!("token response had no token field"))
    }
}

/// Extract `key="value"` from a `WWW-Authenticate` challenge string.
fn kv(challenge: &str, key: &str) -> Option<String> {
    let needle = format!("{key}=\"");
    let start = challenge.find(&needle)? + needle.len();
    let rest = &challenge[start..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// Parse a `Link` header, returning the absolute `rel="next"` URL if present.
fn next_link(link: Option<&str>, endpoint: &str) -> Option<String> {
    let link = link?;
    if !link.contains("rel=\"next\"") && !link.contains("rel=next") {
        return None;
    }
    let lt = link.find('<')?;
    let gt = link[lt + 1..].find('>')? + lt + 1;
    let target = &link[lt + 1..gt];
    if target.starts_with("http://") || target.starts_with("https://") {
        Some(target.to_string())
    } else {
        Some(format!("https://{endpoint}{target}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_challenge_fields() {
        let c = r#"Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/postgres:pull""#;
        assert_eq!(
            kv(c, "realm").as_deref(),
            Some("https://auth.docker.io/token")
        );
        assert_eq!(kv(c, "service").as_deref(), Some("registry.docker.io"));
        assert_eq!(
            kv(c, "scope").as_deref(),
            Some("repository:library/postgres:pull")
        );
        assert_eq!(kv(c, "missing"), None);
    }

    #[test]
    fn ecr_aws_scope() {
        let c =
            r#"Bearer realm="https://public.ecr.aws/token/",service="public.ecr.aws",scope="aws""#;
        assert_eq!(kv(c, "scope").as_deref(), Some("aws"));
    }

    #[test]
    fn link_header_relative_and_absolute() {
        let rel = r#"</v2/library/postgres/tags/list?n=1000&last=foo>; rel="next""#;
        assert_eq!(
            next_link(Some(rel), "registry-1.docker.io").as_deref(),
            Some("https://registry-1.docker.io/v2/library/postgres/tags/list?n=1000&last=foo")
        );
        let abs = r#"<https://gcr.io/v2/x/tags/list?last=y>; rel="next""#;
        assert_eq!(
            next_link(Some(abs), "gcr.io").as_deref(),
            Some("https://gcr.io/v2/x/tags/list?last=y")
        );
        assert_eq!(next_link(None, "x"), None);
        assert_eq!(next_link(Some("<...>; rel=\"prev\""), "x"), None);
    }
}
