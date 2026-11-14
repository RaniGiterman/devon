#!/usr/bin/env bash
# Write kubeconfigs for the agent's two identities into .work/ (git-ignored).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CTX=kind-devon
mkdir -p "$ROOT/.work"
SERVER=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CTX\")].cluster.server}")
CA=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CTX\")].cluster.certificate-authority-data}")
for sa in devon-agent devon-gate; do
  TOKEN=$(kubectl --context "$CTX" -n devon-system create token "$sa" --duration=168h)
  cat > "$ROOT/.work/$sa.kubeconfig" <<KC
apiVersion: v1
kind: Config
clusters: [{name: devon, cluster: {server: $SERVER, certificate-authority-data: $CA}}]
users: [{name: $sa, user: {token: $TOKEN}}]
contexts: [{name: $sa, context: {cluster: devon, user: $sa, namespace: demo}}]
current-context: $sa
KC
  chmod 600 "$ROOT/.work/$sa.kubeconfig"
  echo "    wrote .work/$sa.kubeconfig"
done
