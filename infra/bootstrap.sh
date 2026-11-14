#!/usr/bin/env bash
# Bring up the full demo platform on kind. Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
CLUSTER=devon
CTX=kind-$CLUSTER
GITOPS_REPO_URL=${GITOPS_REPO_URL:?set GITOPS_REPO_URL (see .env)}
ARGOCD_VERSION=v3.3.18
KPS_VERSION=93.2.2
k() { kubectl --context "$CTX" "$@"; }

echo "==> cluster"
kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config kind.yaml

echo "==> images"
docker image inspect devon-demo:1.0.0 >/dev/null 2>&1 || docker build -q -t devon-demo:1.0.0 --build-arg APP_VERSION=1.0.0 "$ROOT/demo-app"
docker image inspect devon-demo:1.1.0 >/dev/null 2>&1 || docker build -q -t devon-demo:1.1.0 --build-arg APP_VERSION=1.1.0 --build-arg BUGGY=1 "$ROOT/demo-app"
docker image inspect redis:7.2.0 >/dev/null 2>&1 || docker pull -q redis:7.2.0
# `kind load` fails on multi-platform images from Docker's containerd store; import one platform instead.
for img in devon-demo:1.0.0 devon-demo:1.1.0 redis:7.2.0; do
  docker save --platform linux/amd64 "$img" | docker exec -i "$CLUSTER-control-plane" ctr -n k8s.io images import --digests --snapshotter=overlayfs - >/dev/null
done

echo "==> storage (local-path: allow volume expansion where supported)"
k patch storageclass standard -p '{"allowVolumeExpansion": true}' >/dev/null || true

echo "==> prometheus + alertmanager"
GATEWAY=$(docker network inspect kind -f '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$' | head -1)
AGENT_WEBHOOK="http://${GATEWAY}:${AGENT_PORT:-8088}/alerts"
echo "    alert webhook -> $AGENT_WEBHOOK"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
sed "s#AGENT_WEBHOOK#${AGENT_WEBHOOK}#" prometheus-values.yaml > /tmp/devon-prom-values.yaml
helm --kube-context "$CTX" upgrade --install kps prometheus-community/kube-prometheus-stack \
  --version "$KPS_VERSION" -n monitoring --create-namespace -f /tmp/devon-prom-values.yaml --wait --timeout 10m

echo "==> argocd"
k create namespace argocd --dry-run=client -o yaml | k apply -f - >/dev/null
k apply -n argocd --server-side --force-conflicts -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml" >/dev/null
# Poll git every 20s instead of 3 min, so fixes land quickly in the demo.
k -n argocd patch configmap argocd-cm --type merge -p '{"data":{"timeout.reconciliation":"20s"}}'
k -n argocd patch svc argocd-server -p '{"spec":{"type":"NodePort","ports":[{"name":"https","port":443,"targetPort":8080,"nodePort":30443}]}}' >/dev/null
k -n argocd rollout restart deploy/argocd-repo-server statefulset/argocd-application-controller >/dev/null
k -n argocd rollout status deploy/argocd-server --timeout=5m
k -n argocd rollout status statefulset/argocd-application-controller --timeout=5m

echo "==> agent identities"
k apply -f rbac/agent.yaml >/dev/null 2>&1 || true   # demo ns may not exist yet on first run

echo "==> demo application"
sed "s#GITOPS_REPO_URL#${GITOPS_REPO_URL}#" argocd/app.yaml | k apply -f -
for i in $(seq 1 60); do k get ns demo >/dev/null 2>&1 && break; sleep 5; done
k apply -f rbac/agent.yaml >/dev/null
"$ROOT/infra/kubeconfigs.sh"
k -n demo rollout status deploy/api --timeout=5m
k -n demo rollout status deploy/frontend --timeout=5m

echo
echo "Ready."
echo "  frontend      http://localhost:8000"
echo "  prometheus    http://localhost:9090"
echo "  alertmanager  http://localhost:9093"
echo "  argocd        https://localhost:8443  (admin / $(k -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d))"
