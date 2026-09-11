from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    assert "TimeoutStopSec=25min" in unit

    update = (PROJECT_ROOT / "scripts/update-server.sh").read_text(encoding="utf-8")
    install_unit = update.index(
        'install -m 0644 "${project_root}/deploy/systemd/ai-chat.service"'
    )
    daemon_reload = update.index("systemctl daemon-reload", install_unit)
    stop_website = update.index("systemctl stop ai-chat.service", daemon_reload)
    assert install_unit < daemon_reload < stop_website
    assert "trap restore_website_on_error ERR" in update[:stop_website]
    assert "systemctl start ai-chat.service || true" in update
