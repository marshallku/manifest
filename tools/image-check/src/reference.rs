//! Parse a container image reference into registry endpoint, repository and tag,
//! following Docker's reference grammar (the part that matters here).
//!
//! Key rule (the one that trips people up): the first path segment is a registry
//! host only if it contains a `.` or `:` or equals `localhost`. Otherwise it is a
//! Docker Hub namespace — so `portainer/portainer-ce` is the Hub repo
//! `portainer/portainer-ce`, NOT a registry called `portainer`.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reference {
    /// Original ref as written in the manifest, e.g. `grafana/grafana:12.3.2`.
    pub raw: String,
    /// Network endpoint to hit for the registry v2 API, e.g.
    /// `registry-1.docker.io`, `quay.io`, `public.ecr.aws`.
    pub endpoint: String,
    /// Repository path used in `/v2/<repo>/tags/list`, e.g. `library/postgres`.
    pub repository: String,
    /// Tag, or `None` when the ref pins a digest or omits a tag.
    pub tag: Option<String>,
}

const DOCKER_ENDPOINT: &str = "registry-1.docker.io";

pub fn parse(raw: &str) -> Reference {
    let raw = raw.trim();

    // Drop a digest pin (`name@sha256:...`); we only version-check by tag.
    let (name_tag, _digest) = match raw.split_once('@') {
        Some((n, d)) => (n, Some(d)),
        None => (raw, None),
    };

    // Split off the tag: the last ':' that is not part of a host:port (i.e. has
    // no '/' after it). Tags never contain '/'.
    let (name, tag) = match name_tag.rfind(':') {
        Some(i) if !name_tag[i + 1..].contains('/') => {
            (&name_tag[..i], Some(name_tag[i + 1..].to_string()))
        }
        _ => (name_tag, None),
    };

    // Decide whether the first segment is a registry host.
    let (mut endpoint, mut repository) = match name.split_once('/') {
        Some((first, rest))
            if first.contains('.') || first.contains(':') || first == "localhost" =>
        {
            (first.to_string(), rest.to_string())
        }
        _ => (DOCKER_ENDPOINT.to_string(), name.to_string()),
    };

    // Canonicalize Docker Hub aliases, and give official (single-segment) Hub
    // images their implicit `library/` namespace.
    if matches!(
        endpoint.as_str(),
        "docker.io" | "index.docker.io" | DOCKER_ENDPOINT
    ) {
        endpoint = DOCKER_ENDPOINT.to_string();
        if !repository.contains('/') {
            repository = format!("library/{repository}");
        }
    }

    Reference {
        raw: raw.to_string(),
        endpoint,
        repository,
        tag,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(s: &str) -> (String, String, Option<String>) {
        let r = parse(s);
        (r.endpoint, r.repository, r.tag)
    }

    #[test]
    fn docker_official_single_segment() {
        assert_eq!(
            p("postgres:16-alpine"),
            (
                DOCKER_ENDPOINT.into(),
                "library/postgres".into(),
                Some("16-alpine".into())
            )
        );
        assert_eq!(
            p("busybox:1.37"),
            (
                DOCKER_ENDPOINT.into(),
                "library/busybox".into(),
                Some("1.37".into())
            )
        );
    }

    #[test]
    fn docker_hub_namespace_is_not_a_registry() {
        // The C3 case from review: `portainer` is a namespace, not a host.
        assert_eq!(
            p("portainer/portainer-ce:2.33.6-alpine"),
            (
                DOCKER_ENDPOINT.into(),
                "portainer/portainer-ce".into(),
                Some("2.33.6-alpine".into())
            )
        );
        assert_eq!(
            p("grafana/grafana:12.3.2"),
            (
                DOCKER_ENDPOINT.into(),
                "grafana/grafana".into(),
                Some("12.3.2".into())
            )
        );
        assert_eq!(
            p("prom/prometheus:v3.9.1"),
            (
                DOCKER_ENDPOINT.into(),
                "prom/prometheus".into(),
                Some("v3.9.1".into())
            )
        );
        assert_eq!(
            p("adguard/adguardhome:v0.107.71"),
            (
                DOCKER_ENDPOINT.into(),
                "adguard/adguardhome".into(),
                Some("v0.107.71".into())
            )
        );
    }

    #[test]
    fn explicit_registries() {
        assert_eq!(
            p("quay.io/argoproj/argocd:v3.3.5"),
            (
                "quay.io".into(),
                "argoproj/argocd".into(),
                Some("v3.3.5".into())
            )
        );
        assert_eq!(
            p("gcr.io/cadvisor/cadvisor:v0.55.1"),
            (
                "gcr.io".into(),
                "cadvisor/cadvisor".into(),
                Some("v0.55.1".into())
            )
        );
        assert_eq!(
            p("docker.n8n.io/n8nio/n8n:2.7.1"),
            (
                "docker.n8n.io".into(),
                "n8nio/n8n".into(),
                Some("2.7.1".into())
            )
        );
        assert_eq!(
            p("public.ecr.aws/docker/library/redis:8.2.3-alpine"),
            (
                "public.ecr.aws".into(),
                "docker/library/redis".into(),
                Some("8.2.3-alpine".into())
            )
        );
        assert_eq!(
            p("docker.io/bitnami/sealed-secrets-controller:0.36.1"),
            (
                DOCKER_ENDPOINT.into(),
                "bitnami/sealed-secrets-controller".into(),
                Some("0.36.1".into())
            )
        );
    }

    #[test]
    fn digest_and_missing_tag() {
        assert_eq!(parse("redis@sha256:abc123").tag, None);
        assert_eq!(parse("redis").tag, None);
        assert_eq!(
            p("redis@sha256:abc"),
            (DOCKER_ENDPOINT.into(), "library/redis".into(), None)
        );
    }

    #[test]
    fn host_with_port() {
        assert_eq!(
            p("localhost:5000/team/app:1.2.3"),
            (
                "localhost:5000".into(),
                "team/app".into(),
                Some("1.2.3".into())
            )
        );
    }
}
