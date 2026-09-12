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
nginx_site=/etc/nginx/sites-available/ai-chat.conf
nginx_backup=
deployment_snapshot=
deployment_mutated=false
update_complete=false
restore_started=false

if systemctl is-active --quiet ai-chat-image-bridge.service; then
  image_bridge_active=true
fi
if systemctl is-active --quiet ai-chat.service; then
  website_active=true
fi

restore_website_on_error() {
  exit_code=$?
  if [[ ${update_complete} == true ]]; then
    return
  fi
  if [[ ${restore_started} == true ]]; then
    exit "${exit_code}"
  fi
  restore_started=true
  trap - ERR EXIT INT TERM
  if [[ ${exit_code} -eq 0 ]]; then
    exit_code=1
  fi
  snapshot_restored=false
  if [[ ${deployment_mutated} == true && -n ${deployment_snapshot} ]]; then
    echo "Update failed; stopping partial deployment before code rollback." >&2
    systemctl stop ai-chat.service || true
    systemctl stop ai-chat-image-bridge.service || true
    if python3 "${project_root}/scripts/deployment-snapshot.py" restore \
      --install-root "${install_root}" --snapshot "${deployment_snapshot}"; then
      chown -R ai-chat:ai-chat \
        "${install_root}/current/website" \
        "${install_root}/current/services/image-bridge" \
        "${install_root}/current/scripts" || true
      snapshot_restored=true
    else
      echo "Code rollback failed; the deployment snapshot was retained at ${deployment_snapshot}." >&2
    fi
  fi
  if [[ -n ${nginx_backup} && -f ${nginx_backup} ]]; then
    echo "Update failed; restoring the previous Nginx configuration." >&2
    install -m 0644 "${nginx_backup}" "${nginx_site}" || true
    nginx -t && systemctl reload nginx || true
    rm -f "${nginx_backup}"
  fi
  if [[ ${snapshot_restored} == true ]]; then
    python3 "${project_root}/scripts/deployment-snapshot.py" discard \
      --install-root "${install_root}" --snapshot "${deployment_snapshot}" || true
    deployment_snapshot=
  elif [[ ${deployment_mutated} == false && -n ${deployment_snapshot} ]]; then
    python3 "${project_root}/scripts/deployment-snapshot.py" discard \
      --install-root "${install_root}" --snapshot "${deployment_snapshot}" || true
    deployment_snapshot=
  fi
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

trap restore_website_on_error ERR EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

deployment_snapshot=$(
  python3 "${project_root}/scripts/deployment-snapshot.py" create \
    --install-root "${install_root}"
)

# Load both current service definitions before stopping or restarting either
# process so systemd applies their graceful-stop windows to active work.
install -m 0644 "${project_root}/deploy/systemd/ai-chat.service" \
  /etc/systemd/system/ai-chat.service
install -m 0644 "${project_root}/deploy/systemd/ai-chat-image-bridge.service" \
  /etc/systemd/system/ai-chat-image-bridge.service
install -m 0644 "${project_root}/deploy/systemd/ai-chat-upload-cleanup.service" \
  /etc/systemd/system/ai-chat-upload-cleanup.service
install -m 0644 "${project_root}/deploy/systemd/ai-chat-upload-cleanup.timer" \
  /etc/systemd/system/ai-chat-upload-cleanup.timer
systemctl daemon-reload
systemctl stop ai-chat.service
if [[ ${image_bridge_active} == true ]]; then
  # The website is stopped first so it cannot enqueue new image work. Then
  # let the bridge drain any request that was already in flight before its
  # source files are replaced.
  systemctl stop ai-chat-image-bridge.service
fi
install -d -o ai-chat -g ai-chat -m 0750 \
  "${install_root}/current/services" "${install_root}/current/scripts"
deployment_mutated=true
rsync -a --delete \
  --exclude data --exclude workspaces --exclude __pycache__ --exclude '*.pyc' \
  "${project_root}/website/" "${install_root}/current/website/"
rsync -a --delete "${project_root}/services/image-bridge/" \
  "${install_root}/current/services/image-bridge/"
install -m 0755 "${project_root}/scripts/cleanup-stale-uploads.py" \
  "${install_root}/current/scripts/cleanup-stale-uploads.py"
install -m 0755 "${project_root}/scripts/deployment-snapshot.py" \
  "${install_root}/current/scripts/deployment-snapshot.py"
chown -R ai-chat:ai-chat "${install_root}/current"
"${install_root}/venv/bin/pip" install -r "${install_root}/current/website/requirements.txt"
if [[ ${image_bridge_active} == true ]]; then
  systemctl start ai-chat-image-bridge.service
  systemctl is-active --quiet ai-chat-image-bridge.service
fi
systemctl start ai-chat.service
systemctl is-active --quiet ai-chat.service
systemctl enable --now ai-chat-upload-cleanup.timer

# Refresh managed routes and upload/streaming limits while retaining any
# Certbot or operator-managed TLS directives in the existing server block.
if [[ ! -f ${nginx_site} ]]; then
  echo "Nginx site configuration is missing: ${nginx_site}" >&2
  exit 1
fi
nginx_backup=$(mktemp)
cp -a "${nginx_site}" "${nginx_backup}"
python3 "${project_root}/scripts/sync-nginx-config.py" \
  "${project_root}/deploy/nginx/ai-chat.conf" "${nginx_site}"
nginx -t
systemctl reload nginx
rm -f "${nginx_backup}"
nginx_backup=
if ! python3 "${project_root}/scripts/deployment-snapshot.py" discard \
  --install-root "${install_root}" --snapshot "${deployment_snapshot}"; then
  echo "Update succeeded, but the old deployment snapshot could not be removed: ${deployment_snapshot}" >&2
else
  deployment_snapshot=
fi
update_complete=true
trap - ERR EXIT INT TERM
echo "Update complete."
