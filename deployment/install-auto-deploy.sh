#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ -f /home/ubuntu/addon/docker-compose.yml ]] || { echo 'Original Compose file missing' >&2; exit 1; }
[[ -f /home/ubuntu/stremio-jackett-deploy/deployment/auto-deploy-addon.sh ]] || { echo 'Clone GitHub repo to /home/ubuntu/stremio-jackett-deploy first' >&2; exit 1; }

# Event-triggered deployment through GitHub Actions replaces the old 5-minute polling service.
systemctl disable --now stremio-jackett-update.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/stremio-jackett-update.timer /etc/systemd/system/stremio-jackett-update.service
systemctl daemon-reload

echo '5-minute polling timer disabled and removed.'
echo 'No production container was changed. Configure GitHub Actions SSH secrets to enable push-triggered deployment.'
echo 'Deployment runs on successful main builds via --deploy-sha; no timer is installed.'
