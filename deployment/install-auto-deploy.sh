#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ -f /home/ubuntu/addon/docker-compose.yml ]] || { echo 'Original Compose file missing' >&2; exit 1; }
[[ -f /home/ubuntu/stremio-jackett-deploy/deployment/auto-deploy-addon.sh ]] || { echo 'Clone GitHub repo to /home/ubuntu/stremio-jackett-deploy first' >&2; exit 1; }
cat >/etc/systemd/system/stremio-jackett-update.service <<'SERVICE'
[Unit]
Description=Check for a verified Stremio Jackett commit and update addon only
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash /home/ubuntu/stremio-jackett-deploy/deployment/auto-deploy-addon.sh --poll
TimeoutStartSec=35min
SERVICE
cat >/etc/systemd/system/stremio-jackett-update.timer <<'TIMER'
[Unit]
Description=Poll verified GitHub commits for Stremio Jackett addon

[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
AccuracySec=1min
Unit=stremio-jackett-update.service

[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now stremio-jackett-update.timer
printf 'Installed timer. Initial deployment is NOT armed. Run sudo bash /home/ubuntu/stremio-jackett-deploy/deployment/auto-deploy-addon.sh --deploy-now AFTER GitHub Actions succeeds.\n'
