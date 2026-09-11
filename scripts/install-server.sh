#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run with sudo: sudo bash scripts/install-server.sh chat.example.com"
  exit 1
fi

domain=${1:-}
if [[ ! ${domain} =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Provide the domain that points to this server, for example: chat.example.com"
  exit 1
fi

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/srv/ai-chat
service_user=ai-chat

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip nginx rsync openssl

if ! id "${service_user}" >/dev/null 2>&1; then
  useradd --system --home "${install_root}" --shell /usr/sbin/nologin "${service_user}"
fi

install -d -o "${service_user}" -g "${service_user}" -m 0750 \
  "${install_root}/current" "${install_root}/data" "${install_root}/workspaces" \
  "${install_root}/state" /etc/ai-chat

rsync -a --delete \
  --exclude data --exclude workspaces --exclude __pycache__ --exclude '*.pyc' \
  "${project_root}/website/" "${install_root}/current/website/"
rsync -a --delete "${project_root}/services/image-bridge/" \
  "${install_root}/current/services/image-bridge/"
chown -R "${service_user}:${service_user}" "${install_root}/current"

python3 -m venv "${install_root}/venv"
"${install_root}/venv/bin/pip" install --upgrade pip
"${install_root}/venv/bin/pip" install -r "${install_root}/current/website/requirements.txt"

if [[ ! -f /etc/ai-chat/website.env ]]; then
  admin_password=$(openssl rand -base64 24 | tr -d '\n')
  session_secret=$(openssl rand -hex 32)
  image_token=$(openssl rand -hex 32)
  transfer_secret=$(openssl rand -hex 32)
  cat > /etc/ai-chat/website.env <<EOF
AI_CHAT_ADMIN_USERNAME=admin
AI_CHAT_ACCESS_PASSWORD=${admin_password}
AI_CHAT_SESSION_SECRET=${session_secret}
AI_CHAT_PUBLIC_BASE_URL=https://${domain}
AI_CHAT_TRANSFER_SECRET=${transfer_secret}
AI_CHAT_DATA_DIR=${install_root}/data
AI_CHAT_WORKSPACE_ROOT=${install_root}/workspaces
CODEX_HOME=${install_root}/data/codex
CODEX_BIN=codex
AI_CHAT_UPSTREAM_URL=http://127.0.0.1:9000
AI_CHAT_RESTRICTED_API_KEY=
AI_CHAT_RESTRICTED_API_MODEL=gpt-5.3-codex-spark
AI_CHAT_IMAGE_BRIDGE_URL=http://127.0.0.1:13003
AI_CHAT_IMAGE_BRIDGE_TOKEN=${image_token}
EOF
  chmod 0600 /etc/ai-chat/website.env
  echo "Initial administrator username: admin"
  echo "Initial administrator password: ${admin_password}"
  echo "Save this password now. It is not written to the Git repository."
fi

install -m 0644 "${project_root}/deploy/systemd/ai-chat.service" /etc/systemd/system/ai-chat.service
sed "s/chat\.example\.com/${domain}/g" "${project_root}/deploy/nginx/ai-chat.conf" \
  > /etc/nginx/sites-available/ai-chat.conf
ln -sfn /etc/nginx/sites-available/ai-chat.conf /etc/nginx/sites-enabled/ai-chat.conf
rm -f /etc/nginx/sites-enabled/default

systemctl daemon-reload
systemctl enable --now ai-chat.service
nginx -t
systemctl reload nginx

echo "Website installed. Next run: sudo certbot --nginx -d ${domain}"
