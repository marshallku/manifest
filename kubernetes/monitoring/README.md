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

## 3. Loki

kubectl apply -f kubernetes/monitoring/loki/

## 4. Prometheus (RBAC 포함)

kubectl apply -f kubernetes/monitoring/prometheus/

## 5. Alloy (Loki 필요)

kubectl apply -f kubernetes/monitoring/alloy/

## 6. Grafana (Prometheus + Loki 필요)

kubectl apply -f kubernetes/monitoring/grafana/
