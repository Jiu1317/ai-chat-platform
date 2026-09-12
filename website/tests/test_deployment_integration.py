import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_update_script_passes_exactly_template_and_installed_nginx_paths() -> None:
    source = (PROJECT_ROOT / "scripts" / "update-server.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'python3 "${project_root}/scripts/sync-nginx-config.py" \\\n'
        '  "${project_root}/deploy/nginx/ai-chat.conf" "${nginx_site}"'
    ) in source
    assert 'sync-nginx-config.py" +' not in source


def _location_block(config: str, declaration: str) -> str:
    start = config.index(declaration)
    end = config.index("\n    }", start)
    return config[start:end]


def test_nginx_streams_regular_and_chunked_upload_routes() -> None:
    config = (PROJECT_ROOT / "deploy/nginx/ai-chat.conf").read_text(encoding="utf-8")
    for declaration in (
        "location = /api/uploads {",
        "location ^~ /api/uploads/ {",
    ):
        block = _location_block(config, declaration)
        assert "client_max_body_size 31m;" in block
        assert "proxy_request_buffering off;" in block
        assert "proxy_buffering off;" in block
        assert "proxy_read_timeout 600s;" in block
        assert "proxy_send_timeout 600s;" in block


def test_nginx_streams_transfer_image_capability_route_without_cache() -> None:
    config = (PROJECT_ROOT / "deploy/nginx/ai-chat.conf").read_text(encoding="utf-8")
    block = _location_block(config, "location ^~ /api/transfer-image/ {")
    assert "proxy_buffering off;" in block
    assert "proxy_request_buffering off;" in block
    assert "proxy_cache off;" in block
    assert "proxy_read_timeout 600s;" in block
    assert "proxy_send_timeout 600s;" in block


def test_windows_start_scripts_roll_back_partial_startup() -> None:
    for relative_path in (
        "bridge/windows/start-dual-tab.ps1",
        "standalone/web2api-chrome/start-dual-tab.ps1",
    ):
        script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "$startedProcesses = @()" in script
        assert "$startedProcesses += $worker1" in script
        assert "$startedProcesses += $worker2" in script
        assert "$startedProcesses += $gateway" in script
        assert "foreach ($startedProcess in $startedProcesses)" in script
        assert "Stop-Process -Id $startedProcess.Id -Force" in script
        assert "Remove-Item -LiteralPath $ProcessStateFile" in script


def test_server_update_loads_graceful_stop_window_before_stopping() -> None:
    unit = (PROJECT_ROOT / "deploy/systemd/ai-chat.service").read_text(
        encoding="utf-8"
    )
    assert "TimeoutStopSec=46min" in unit
    assert "44-minute drain deadline" in unit

    update = (PROJECT_ROOT / "scripts/update-server.sh").read_text(encoding="utf-8")
    install_unit = update.index(
        'install -m 0644 "${project_root}/deploy/systemd/ai-chat.service"'
    )
    daemon_reload = update.index("systemctl daemon-reload", install_unit)
    stop_website = update.index("systemctl stop ai-chat.service", daemon_reload)
    assert install_unit < daemon_reload < stop_website
    assert "trap restore_website_on_error ERR" in update[:stop_website]
    assert "systemctl start ai-chat.service || true" in update


def test_server_update_syncs_nginx_routes_without_losing_tls_settings() -> None:
    update = (PROJECT_ROOT / "scripts/update-server.sh").read_text(encoding="utf-8")
    assert "sync-nginx-config.py" in update
    assert "nginx -t" in update
    assert "systemctl reload nginx" in update
    assert "restoring the previous Nginx configuration" in update

    template = PROJECT_ROOT / "deploy/nginx/ai-chat.conf"
    installed_text = template.read_text(encoding="utf-8")
    installed_text = installed_text.replace(
        "    client_max_body_size 55m;",
        "    listen 443 ssl;\n"
        "    ssl_certificate /etc/letsencrypt/live/chat.example.com/fullchain.pem;\n"
        "    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;\n"
        "    client_max_body_size 10m;",
    ).replace(
        "        client_max_body_size 31m;",
        "        client_max_body_size 8m;",
    )
    installed_text += (
        "\nserver {\n"
        "    listen 80;\n"
        "    server_name chat.example.com;\n"
        "    return 301 https://$host$request_uri;\n"
        "}\n"
    )

    with tempfile.TemporaryDirectory() as directory:
        installed = Path(directory) / "ai-chat.conf"
        installed.write_text(installed_text, encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts/sync-nginx-config.py"),
                str(template),
                str(installed),
            ],
            check=True,
        )
        result = installed.read_text(encoding="utf-8")

    assert "listen 443 ssl;" in result
    assert "ssl_certificate /etc/letsencrypt/live/chat.example.com/fullchain.pem;" in result
    assert "return 301 https://$host$request_uri;" in result
    assert result.count("location = /api/uploads {") == 1
    assert result.count("location ^~ /api/uploads/ {") == 1
    assert result.count("location ^~ /api/transfer-image/ {") == 1
    assert result.count("client_max_body_size 31m;") == 2
    assert "client_max_body_size 8m;" not in result
