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
MODE="${1:-}"
mkdir -p "$STATE_DIR"
exec 9>"$LOCK_FILE"
flock -w 900 9 || { echo "Another deployment is running; lock timeout" >&2; exit 1; }

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
compose() {
  if [[ -f "$OVERRIDE_FILE" ]]; then
    docker compose --project-directory "$COMPOSE_DIR" -p "$PROJECT" -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" "$@"
  else
    docker compose --project-directory "$COMPOSE_DIR" -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
  fi
}
assert_current_stack() {
  [[ -f "$COMPOSE_FILE" ]] || { log "ERROR: Missing original Compose file: $COMPOSE_FILE"; return 1; }
  docker inspect "$SERVICE" >/dev/null || { log "ERROR: Running addon container not found"; return 1; }
  if docker exec "$SERVICE" test -f /app/.env; then
    log "ERROR: /app/.env exists inside the container; migrate its values to the private Compose environment before deploying."
    return 1
  fi
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
  python3 -c 'import json,sys
data=json.load(sys.stdin)
service=data["services"]["addon"]
labels=service.get("labels",{})
if isinstance(labels,list):
    labels=dict(row.split("=",1) for row in labels)
required={
    "traefik.http.routers.addon.rule":"Host(`hsp99.duckdns.org`)",
    "traefik.docker.network":"traefik_default",
    "traefik.http.services.addon.loadbalancer.server.port":"3000",
}
for k,v in required.items():
    assert str(labels.get(k))==v, f"Original Compose missing required Traefik label: {k}"
networks=service.get("networks",{})
assert "traefik_default" in networks, "Original Compose missing traefik_default network"
print("Compose routing and network match required production settings")
' <<<"$effective"
}
latest_commit() {
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --depth 1 --branch main "$REPO_URL" "$REPO_DIR" >&2
  fi
  git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" fetch -q --depth 1 origin main
  git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" rev-parse FETCH_HEAD
}
ci_success() {
  local sha="$1" result
  result="$(python3 - "$sha" "$REPO_SLUG" <<'PY'
import json,sys,urllib.request,urllib.parse
sha,slug=sys.argv[1:]
url=f'https://api.github.com/repos/{slug}/actions/workflows/build-and-push.yml/runs?'+urllib.parse.urlencode({'head_sha':sha,'per_page':'30'})
request=urllib.request.Request(url,headers={'User-Agent':'jackett-vps-deployer','Accept':'application/vnd.github+json'})
with urllib.request.urlopen(request,timeout=20) as response:
    data=json.load(response)
runs=[r for r in data.get('workflow_runs',[]) if r.get('head_sha')==sha and r.get('event') in ('push','workflow_dispatch') and r.get('head_branch')=='main']
print('success' if any(r.get('status')=='completed' and r.get('conclusion')=='success' for r in runs) else 'pending_or_failed')
PY
)" || { log "Could not verify GitHub Actions status; deployment skipped"; return 1; }
  [[ "$result" == success ]]
}
container_ok() {
  local name="$1" n
  for n in 1 2 3 4 5 6 7 8 9 10; do
    if docker exec "$name" python -c '
import json, urllib.request
base = "http://127.0.0.1:3000"
with urllib.request.urlopen(base + "/manifest.json", timeout=3) as response:
    assert "stream" in json.load(response).get("resources", [])
with urllib.request.urlopen(base + "/configure", timeout=3) as response:
    assert b"</html>" in response.read().lower(), "Configuration HTML missing"
with urllib.request.urlopen(base + "/static/config.js", timeout=3) as response:
    assert response.status == 200
' >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}
https_ok() {
  local n
  for n in 1 2 3 4 5 6; do
    if curl --fail --silent --show-error --max-time 10 'https://hsp99.duckdns.org/manifest.json' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "stream" in d.get("resources",[])' >/dev/null 2>&1 \
      && curl --fail --silent --show-error --max-time 10 -o /dev/null 'https://hsp99.duckdns.org/configure' \
      && curl --fail --silent --show-error --max-time 10 -o /dev/null 'https://hsp99.duckdns.org/static/config.js'; then
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
  local sha="$1" image candidate backupdir previous_image had_override=false
  image="${IMAGE_PREFIX}:${sha}"
  candidate="stremio-jackett-canary-$$"
  log "Using verified ARM64 CI image ${image} (no Docker rebuild)"
  git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" checkout --force --detach "$sha" >/dev/null
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    log "ERROR: Prebuilt image $image is missing. Import the verified CI image first."
    return 1
  fi
  if [[ "$(docker image inspect "$image" --format '{{.Architecture}}')" != "arm64" ]]; then
    log "ERROR: Prebuilt image $image is not ARM64"
    return 1
  fi
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
  previous_image="$(docker inspect addon --format '{{.Config.Image}}')"
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
  # Pin the exact image that was running before the attempted deployment.
  write_override "$previous_image"
  if compose up -d --no-deps --no-build --force-recreate addon && container_ok addon; then
    log "Rollback container restored"
  else
    log "CRITICAL: Rollback failed; manual intervention required"
  fi
  return 1
}

case "$MODE" in
  --deploy-sha)
    [[ $# -eq 2 && "${2:-}" =~ ^[0-9a-f]{40}$ ]] || { echo 'Usage: --deploy-sha FULL_40_CHAR_COMMIT_SHA' >&2; exit 2; }
    sha="$2"
    ;;
  --deploy-now)
    [[ $# -eq 1 ]] || { echo 'Usage: --deploy-now' >&2; exit 2; }
    ;;
  *)
    echo 'Usage: auto-deploy-addon.sh --deploy-sha FULL_40_CHAR_COMMIT_SHA | --deploy-now' >&2
    exit 2
    ;;
esac
assert_current_stack
head_sha="$(latest_commit)"
if [[ "$MODE" == --deploy-sha ]]; then
  if [[ "$sha" != "$head_sha" ]]; then
    log "SKIPPED: $sha has been superseded by main $head_sha; do not deploy an outdated commit"
    exit 0
  fi
  # GitHub Actions invokes this mode ONLY after the dependent Docker build job succeeds.
else
  sha="$head_sha"
  if ! ci_success "$sha"; then
    log "ERROR: No completed successful GitHub Actions build for $sha"
    exit 1
  fi
fi
if [[ -f "$LAST_FILE" && "$(cat "$LAST_FILE")" == "$sha" ]]; then
  log "Already deployed $sha"
  exit 0
fi
deploy "$sha"
