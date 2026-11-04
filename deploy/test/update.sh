#!/bin/sh
# Pulls the newest image and restarts the bot only if it changed.
# Run by cron every few minutes (see README); safe to run by hand too.
set -eu
cd "$(dirname "$0")"
docker compose pull --quiet
before=$(docker inspect --format '{{.Image}}' crusader-test 2>/dev/null || echo none)
docker compose up -d --remove-orphans
after=$(docker inspect --format '{{.Image}}' crusader-test)
if [ "$before" != "$after" ]; then
  version=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' crusader-test | sed -n 's/^APP_VERSION=//p')
  echo "$(date -u '+%F %T') updated crusader-test to build ${version:-unknown}"
  docker image prune -f >/dev/null
fi
