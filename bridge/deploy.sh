#!/usr/bin/env bash
# Copy the bridge to a Docker host on your LAN, build and (re)start it.
#   DEPLOY_HOST=user@192.168.1.10 ./deploy.sh
#   DEPLOY_HOST=user@host REMOTE_DIR=~/sonos-bridge PROFILES="--profile tunnel" ./deploy.sh
set -euo pipefail
: "${DEPLOY_HOST:?set DEPLOY_HOST=user@host}"
REMOTE_DIR="${REMOTE_DIR:-~/sonos-bridge}"
PROFILES="${PROFILES:-}"
cd "$(dirname "$0")"
[ -f .env ] || { echo ".env missing: cp .env.example .env and fill it in"; exit 1; }
rsync -az --delete --exclude cache --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
      ./ "$DEPLOY_HOST:$REMOTE_DIR/"
ssh "$DEPLOY_HOST" "chmod 600 $REMOTE_DIR/.env && cd $REMOTE_DIR && docker compose $PROFILES up -d --build && sleep 3 && docker compose logs --tail 20"
