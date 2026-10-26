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

네트워크는 `Homelab Network` 대시보드 한 장이다. 데이터 출처는 셋이다.
- 회선 품질: `smokeping/`(ICMP 1초)
- WAN·세그먼트·VM 트래픽: pve02 의 Debian `prometheus-node-exporter`가 읽는 tap 카운터(infra `bootstrap/pve02.sh`)
- 스위치 포트: `snmp-exporter/`(SNMP v2c 읽기 전용 커뮤니티 — SealedSecret `snmp-exporter-auth`)

OPNsense 자체 exporter(os-node_exporter)는 펌웨어 업데이트를 먼저 요구해서 쓰지 않는다.
업데이트 중에 재부팅되면 집 전체가 끊긴다.

대시보드는 둘로 나뉜다 — 클러스터 워크로드는 `container-monitoring`
(namespace/pod/container 라벨), app01의 Docker 컨테이너는 `docker-monitoring`
(`name` 라벨). 라벨 스키마가 달라서 한 장으로 합칠 수 없다.

## 제품 이벤트 (telemetry)

앱 공장의 제품 이벤트는 Loki가 아니라 analytics01의 ClickHouse에 있다
(`docker-compose/analytics01/telemetry/`). Grafana는 `ClickHouse Telemetry` 데이터소스
(`grafana_ro`, HTTP 8123, 플러그인 4.22.1을 `GF_PLUGINS_PREINSTALL_SYNC`로 고정)로 읽고,
Prometheus는 수집기 `/metrics`를 job `telemetry`로 긁는다. 대시보드 두 장은
`grafana/telemetry-dashboards.py`가 만들고 `grafana/check-telemetry-dashboards.py`가 모든 패널을
Grafana 쿼리 API로 실행해 확인한다.

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

## 5b. 네트워크 exporter

kubectl apply -f kubernetes/monitoring/smokeping/ -f kubernetes/monitoring/snmp-exporter/

## 6. Grafana (Prometheus + Loki 필요)

kubectl apply -f kubernetes/monitoring/grafana/
