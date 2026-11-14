#!/usr/bin/env bash
# Create the gitops-manifests repo on GitHub (if needed) and push the seed content.
# Tags the seeded state as `baseline` so `chaos reset` can return to it.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO=${GITOPS_REPO:?set GITOPS_REPO, e.g. RaniGiterman/devon-gitops}
DIR="$ROOT/.work/seed"
if ! gh repo view "$REPO" >/dev/null 2>&1; then
  gh repo create "$REPO" --public --description "GitOps manifests for the devon self-healing demo"
fi
if git ls-remote --exit-code --heads "https://github.com/$REPO.git" main >/dev/null 2>&1; then
  echo "$REPO already has a main branch; not overwriting it. Use 'chaos reset' to return to baseline."
  exit 0
fi
rm -rf "$DIR" && mkdir -p "$DIR" && cp -r "$ROOT/infra/gitops-seed/." "$DIR/"
cd "$DIR"
git init -q -b main
git add -A
git -c user.name="devon-bootstrap" -c user.email="devon@localhost" commit -q -m "seed demo manifests and runbooks"
git tag -f baseline
git remote add origin "https://github.com/$REPO.git"
git push -q -u origin main
git push -q origin baseline
echo "seeded https://github.com/$REPO"
