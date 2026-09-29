# Monitoring stack deployment

## 1. Namespace

kubectl apply -f kubernetes/monitoring/namespace.yaml

## 2. DaemonSets (의존성 없음)

kubectl apply -f kubernetes/monitoring/node-exporter/

컨테이너 메트릭은 kubelet 내장 cAdvisor(`/metrics/cadvisor`)를 긁는다. 별도
cAdvisor DaemonSet은 없다 — Docker 소켓을 전제로 동작하는데 노드 런타임이
containerd라서 cgroup 경로만 내보내고 pod/container 라벨이 붙지 않았다.

클러스터 밖 호스트는 각자 exporter를 띄우고 `static_configs`로 긁는다.
app01의 Docker 컨테이너는 `docker-compose/monitoring/`(cAdvisor + node-exporter),
pi01은 `docker-compose/pi01/node-exporter/`, Mac mini는 네이티브 node_exporter.

대시보드는 둘로 나뉜다 — 클러스터 워크로드는 `container-monitoring`
(namespace/pod/container 라벨), app01의 Docker 컨테이너는 `docker-monitoring`
(`name` 라벨). 라벨 스키마가 달라서 한 장으로 합칠 수 없다.

## 로그

Alloy도 같은 이유로 둘이다. 클러스터 파드 로그는 in-cluster Alloy가 Kubernetes
API로 tail 하고(`loki.source.kubernetes`, RBAC에 `pods/log` 필요), app01의 Docker
컨테이너 로그는 `docker-compose/monitoring/config.alloy`가 docker.sock에서 읽어
Loki NodePort(30100)로 밀어 넣는다. 양쪽 다 `host` 라벨을 붙이므로 LogQL 한
셀렉터로 출처를 고를 수 있다: `{host="app01"}`, `{host="k3s01"}`.

Alloy의 `--storage.path`는 **반드시 영속 경로**여야 한다. 컨테이너 파일시스템에
두면 재시작마다 모든 로그를 처음부터 다시 읽고, Loki는 1주(`reject_old_samples_max_age`)
넘은 것을 버리면서 그 사이 per-stream rate limit을 실시간 로그와 나눠 쓰게 된다.
in-cluster는 hostPath `/var/lib/alloy`, app01은 named volume.

## 3. Loki

kubectl apply -f kubernetes/monitoring/loki/

## 4. Prometheus (RBAC 포함)

kubectl apply -f kubernetes/monitoring/prometheus/

## 5. Alloy (Loki 필요)

kubectl apply -f kubernetes/monitoring/alloy/

## 6. Grafana (Prometheus + Loki 필요)

kubectl apply -f kubernetes/monitoring/grafana/
