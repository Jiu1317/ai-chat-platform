#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run with sudo: sudo bash scripts/update-server.sh"
  exit 1
fi

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/srv/ai-chat
image_bridge_active=false
website_active=false

if systemctl is-active --quiet ai-chat-image-bridge.service; then
  image_bridge_active=true
fi
if systemctl is-active --quiet ai-chat.service; then
  website_active=true
fi

restore_website_on_error() {
  exit_code=$?
  if [[ ${image_bridge_active} == true ]]; then
    echo "Update failed; attempting to restore the image bridge service." >&2
    systemctl start ai-chat-image-bridge.service || true
  fi
  if [[ ${website_active} == true ]]; then
    echo "Update failed; attempting to restore the website service." >&2
    systemctl start ai-chat.service || true
  fi
  exit "${exit_code}"
}

trap restore_website_on_error ERR

# Load both current service definitions before stopping or restarting either
# process so systemd applies their graceful-stop windows to active work.
install -m 0644 "${project_root}/deploy/systemd/ai-chat.service" \
  /etc/systemd/system/ai-chat.service
install -m 0644 "${project_root}/deploy/systemd/ai-chat-image-bridge.service" \
  /etc/systemd/system/ai-chat-image-bridge.service
systemctl daemon-reload
systemctl stop ai-chat.service
if [[ ${image_bridge_active} == true ]]; then
  # The website is stopped first so it cannot enqueue new image work. Then
  # let the bridge drain any request that was already in flight before its
  # source files are replaced.
  systemctl stop ai-chat-image-bridge.service
fi
install -d -o ai-chat -g ai-chat -m 0750 "${install_root}/current/services"
rsync -a --delete \
  --exclude data --exclude workspaces --exclude __pycache__ --exclude '*.pyc' \
  "${project_root}/website/" "${install_root}/current/website/"
rsync -a --delete "${project_root}/services/image-bridge/" \
  "${install_root}/current/services/image-bridge/"
chown -R ai-chat:ai-chat "${install_root}/current"
"${install_root}/venv/bin/pip" install -r "${install_root}/current/website/requirements.txt"
if [[ ${image_bridge_active} == true ]]; then
  systemctl start ai-chat-image-bridge.service
  systemctl is-active --quiet ai-chat-image-bridge.service
fi
systemctl start ai-chat.service
systemctl is-active --quiet ai-chat.service
trap - ERR
echo "Update complete."
