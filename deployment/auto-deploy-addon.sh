#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/hsp1020/stremio-jackett.git"
REPO_SLUG="hsp1020/stremio-jackett"
REPO_DIR="/home/ubuntu/stremio-jackett-deploy"
COMPOSE_DIR="/home/ubuntu/addon"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"
OVERRIDE_FILE="${COMPOSE_DIR}/docker-compose.auto.yml"
STATE_DIR="/home/ubuntu/.stremio-jackett-deploy"
LAST_FILE="${STATE_DIR}/deployed-sha"
LOCK_FILE="${STATE_DIR}/lock"
SERVICE="addon"
PROJECT="addon"
IMAGE_PREFIX="stremio-jackett-personal"
MODE="${1:---poll}"
mkdir -p "$STATE_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Deployment check already running"; exit 0; }

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
compose() {
  if [[ -f "$OVERRIDE_FILE" ]]; then
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" "$@"
  else
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
  fi
}
assert_current_stack() {
  [[ -f "$COMPOSE_FILE" ]] || { log "ERROR: Missing original Compose file: $COMPOSE_FILE"; return 1; }
  docker inspect "$SERVICE" >/dev/null || { log "ERROR: Running addon container not found"; return 1; }
  python3 - "$COMPOSE_FILE" "$SERVICE" <<'PY'
import json, subprocess, sys
name = sys.argv[2]
container = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
labels = container['Config']['Labels']
networks = container['NetworkSettings']['Networks']
assert labels.get('com.docker.compose.project') == 'addon', 'Unexpected Compose project'
assert labels.get('com.docker.compose.service') == 'addon', 'Unexpected Compose service'
assert labels.get('traefik.http.routers.addon.rule') == 'Host(`hsp99.duckdns.org`)', 'Unexpected Traefik hostname'
assert labels.get('traefik.docker.network') == 'traefik_default', 'Unexpected Traefik network'
assert labels.get('traefik.http.services.addon.loadbalancer.server.port') == '3000', 'Unexpected addon internal port'
assert 'traefik_default' in networks, 'Traefik network missing'
print('Current addon Compose project, hostname, routing port, and network: OK')
PY
  # Validate effective Compose before we touch anything. Never print env secrets.
  local effective
  effective="$(compose config --format json)" || return 1
  python3 -c 'import sys,json; data=json.load(sys.stdin); assert "addon" in data["services"]' <<<"$effective"
}
latest_commit() {
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --depth 1 --branch main "$REPO_URL" "$REPO_DIR" >&2
  fi
  git -C "$REPO_DIR" fetch -q --depth 1 origin main
  git -C "$REPO_DIR" rev-parse FETCH_HEAD
}
ci_success() {
  local sha="$1" result
  result="$(python3 - "$sha" "$REPO_SLUG" <<'PY'
import json,sys,urllib.request,urllib.parse
sha,slug=sys.argv[1:]
url=f'https://api.github.com/repos/{slug}/actions/workflows/build-and-push.yml/runs?'+urllib.parse.urlencode({'head_sha':sha,'event':'push','per_page':'30'})
request=urllib.request.Request(url,headers={'User-Agent':'jackett-vps-deployer','Accept':'application/vnd.github+json'})
with urllib.request.urlopen(request,timeout=20) as response:
    data=json.load(response)
runs=[r for r in data.get('workflow_runs',[]) if r.get('head_sha')==sha and r.get('event')=='push']
print('success' if any(r.get('status')=='completed' and r.get('conclusion')=='success' for r in runs) else 'pending_or_failed')
PY
)" || { log "Could not verify GitHub Actions status; deployment skipped"; return 1; }
  [[ "$result" == success ]]
}
container_ok() {
  local name="$1" n
  for n in 1 2 3 4 5 6 7 8 9 10; do
    if docker exec "$name" python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:3000/manifest.json",timeout=3)); assert "stream" in d.get("resources",[])' >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}
https_ok() {
  local n
  for n in 1 2 3 4 5 6; do
    if curl --fail --silent --show-error --max-time 10 'https://hsp99.duckdns.org/manifest.json' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "stream" in d.get("resources",[])' >/dev/null 2>&1; then
      return 0
    fi
    sleep 4
  done
  return 1
}
write_override() {
  local image="$1"
  local temp="${OVERRIDE_FILE}.tmp.$$"
  printf 'services:\n  addon:\n    image: "%s"\n' "$image" > "$temp"
  mv "$temp" "$OVERRIDE_FILE"
}
deploy() {
  local sha="$1" image candidate backupdir had_override=false
  image="${IMAGE_PREFIX}:${sha}"
  candidate="stremio-jackett-canary-$$"
  log "Building candidate ${image} (no change to current addon)"
  git -C "$REPO_DIR" checkout --force --detach "$sha" >/dev/null
  docker build --pull -t "$image" -f "$REPO_DIR/source/Dockerfile" "$REPO_DIR/source"
  log "Starting isolated Docker canary"
  docker run -d --rm --name "$candidate" "$image" >/dev/null
  if ! container_ok "$candidate"; then
    log "ERROR: Candidate failed manifest check; existing addon untouched"
    docker rm -f "$candidate" >/dev/null 2>&1 || true
    return 1
  fi
  docker rm -f "$candidate" >/dev/null 2>&1 || true
  log "Candidate OK; backing up current overlay and recreating addon only"
  backupdir="$(mktemp -d "$STATE_DIR/backup.XXXXXXXX")"
  if [[ -f "$OVERRIDE_FILE" ]]; then
    cp -p "$OVERRIDE_FILE" "$backupdir/override.yml"
    had_override=true
  fi
  write_override "$image"
  if compose up -d --no-deps --no-build --force-recreate addon && container_ok addon && https_ok; then
    printf '%s\n' "$sha" >"${LAST_FILE}.tmp"
    mv "${LAST_FILE}.tmp" "$LAST_FILE"
    log "SUCCESS: deployed ${sha}; HTTPS and container verified"
    return 0
  fi
  log "ERROR: New addon failed; rolling back original Compose image"
  if [[ "$had_override" == true ]]; then
    cp -p "$backupdir/override.yml" "$OVERRIDE_FILE"
  else
    rm -f "$OVERRIDE_FILE"
  fi
  if compose up -d --no-deps --no-build --force-recreate addon && container_ok addon; then
    log "Rollback container restored"
  else
    log "CRITICAL: Rollback failed; manual intervention required"
  fi
  return 1
}

case "$MODE" in
  --poll|--deploy-now) ;;
  *) echo 'Usage: auto-deploy-addon.sh [--poll|--deploy-now]' >&2; exit 2 ;;
esac
assert_current_stack
sha="$(latest_commit)"
if [[ "$MODE" == --poll && ! -f "$LAST_FILE" ]]; then
  log 'Not armed. Run --deploy-now once after initial review and GitHub Actions success.'
  exit 0
fi
if [[ "$MODE" == --poll && "$(cat "$LAST_FILE")" == "$sha" ]]; then
  log 'No new commit'; exit 0
fi
if ! ci_success "$sha"; then
  log "GitHub Actions has not succeeded for ${sha}. No deployment."
  exit 0
fi
deploy "$sha"
