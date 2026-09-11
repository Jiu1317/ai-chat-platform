#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run with sudo: sudo bash scripts/update-server.sh"
  exit 1
fi

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/srv/ai-chat

systemctl stop ai-chat.service
rsync -a --delete \
  --exclude data --exclude workspaces --exclude __pycache__ --exclude '*.pyc' \
  "${project_root}/website/" "${install_root}/current/website/"
rsync -a --delete "${project_root}/services/image-bridge/" \
  "${install_root}/current/services/image-bridge/"
chown -R ai-chat:ai-chat "${install_root}/current"
"${install_root}/venv/bin/pip" install -r "${install_root}/current/website/requirements.txt"
systemctl start ai-chat.service
systemctl is-active --quiet ai-chat.service
echo "Update complete."
