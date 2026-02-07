# Monitoring stack deployment

## 1. Namespace

kubectl apply -f kubernetes/monitoring/namespace.yaml

## 2. DaemonSets (의존성 없음)

kubectl apply -f kubernetes/monitoring/node-exporter/
kubectl apply -f kubernetes/monitoring/cadvisor/

## 3. Loki

kubectl apply -f kubernetes/monitoring/loki/

## 4. Prometheus (RBAC 포함)

kubectl apply -f kubernetes/monitoring/prometheus/

## 5. Alloy (Loki 필요)

kubectl apply -f kubernetes/monitoring/alloy/

## 6. Grafana (Prometheus + Loki 필요)

kubectl apply -f kubernetes/monitoring/grafana/
