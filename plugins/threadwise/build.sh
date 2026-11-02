#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "==> Compiling Linux Go binary..."
docker run --rm --platform linux/amd64 \
    -v "${REPO_ROOT}:/workspace" \
    -w /workspace/plugins/threadwise \
    golang:1.26.10 \
    go build -buildvcs=false -mod=mod -trimpath -o server/dist/plugin-linux-amd64 .

echo "==> Packaging threadwise.tar.gz..."
docker run --rm --platform linux/amd64 \
    -v "${REPO_ROOT}:/workspace" \
    -w /workspace/plugins/threadwise \
    golang:1.26.10 \
    sh -c "chmod 755 server/dist/plugin-linux-amd64 && tar --mode='u+rwX,go+rX' -czf threadwise.tar.gz plugin.json server webapp public"

echo "==> Deploying to running Mattermost container..."
docker cp "${SCRIPT_DIR}/threadwise.tar.gz" threadwise-mattermost:/tmp/threadwise.tar.gz
docker exec threadwise-mattermost /mattermost/bin/mmctl plugin add /tmp/threadwise.tar.gz --force --local
docker exec threadwise-mattermost /mattermost/bin/mmctl plugin enable com.threadwise.mattermost --local

echo "==> Plugin built and deployed successfully!"

