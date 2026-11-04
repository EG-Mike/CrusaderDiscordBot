#!/bin/sh
# Pulls the newest image and restarts the bot only if it changed.
# Run by cron every few minutes (see README); safe to run by hand too.
#
# Before restarting for a new build it leaves data/update-pending (the new
# version and its title), so the bot's System log can say "New build
# detected" when it's asked to stop. The bot removes the file when it starts.
set -eu
cd "$(dirname "$0")"
docker compose pull --quiet
image=$(docker compose config --images | head -n 1)
running=$(docker inspect --format '{{.Image}}' crusader-test 2>/dev/null || echo none)
latest=$(docker image inspect --format '{{.Id}}' "$image")
if [ "$running" != "$latest" ]; then
  env=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image")
  version=$(printf '%s\n' "$env" | sed -n 's/^APP_VERSION=//p' | cut -c1-12)
  title=$(printf '%s\n' "$env" | sed -n 's/^APP_COMMIT_TITLE=//p' | tr -d '\t')
  printf '%s\t%s\n' "${version:-unknown}" "$title" > data/update-pending
fi
docker compose up -d --remove-orphans
if [ "$running" != "$latest" ]; then
  echo "$(date -u '+%F %T') updated crusader-test to build ${version:-unknown}"
  docker image prune -f >/dev/null
fi
