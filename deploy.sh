#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
SERVICES=(streamer-qq.service)

cd "$PROJECT_DIR"

if [[ ! -f data/config.json || ! -f data/owner.json ]]; then
  echo "缺少 QQ 配置或绑定资料。请先运行 qq_private.py init 和 listen 完成绑定。" >&2
  exit 1
fi

chmod 700 data
find data -maxdepth 1 -type f -exec chmod 600 {} +

python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v

install_service() {
  local source="$1"
  local target="$2"
  sed \
    -e "s|User=ubuntu|User=${RUN_USER}|" \
    -e "s|/home/ubuntu/StreamerStatusMonitor|${PROJECT_DIR}|g" \
    "$source" > "$TMP_DIR/$target"
  sudo install -m 644 "$TMP_DIR/$target" "/etc/systemd/system/$target"
}

install_service qq-private.service streamer-qq.service

if [[ -f data/twitch.json ]]; then
  install_service twitch-monitor.service streamer-twitch.service
  SERVICES+=(streamer-twitch.service)
fi

if [[ -f data/youtube.json ]]; then
  install_service youtube-monitor.service streamer-youtube.service
  SERVICES+=(streamer-youtube.service)
fi

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICES[@]}"
sudo systemctl restart "${SERVICES[@]}"
sudo systemctl status "${SERVICES[@]}" --no-pager --lines=0
