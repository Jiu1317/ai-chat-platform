import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _load_upload_cleanup_module():
    path = PROJECT_ROOT / "scripts/cleanup-stale-uploads.py"
    spec = importlib.util.spec_from_file_location("cleanup_stale_uploads", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_defers_recovery_for_busy_or_indeterminate_runtime() -> None:
    source = _source("standalone/web2api-chrome/watch-dual-tab.ps1")
    for port in (9181, 9182, 9183, 9325):
        assert f"Test-LocalPort -Port {port}" in source
    assert "return 'busy'" in source
    assert "return 'indeterminate'" in source
    assert "active_asset_transfers -gt 0" in source
    assert "$health.busy -eq $true" in source
    assert "@('healthy', 'busy')" in source
    assert "WEB2API_WATCH_INDETERMINATE_THRESHOLD" in source
    assert "$indeterminateChecks -lt $IndeterminateThreshold" in source
    assert source.index("$condition -in") < source.index("& $StartScript -FromWatchdog")
    assert "$MaxRecoveryAttempts" in source
    assert "Automatic recovery limit reached" in source
    assert "[Math]::Pow(2, $recoveryAttempts - 1)" in source
    assert "[Net.Http.HttpClient]::new($handler)" in source
    assert "Invoke-RestMethod" not in source
    assert "ReadAsStringAsync" in source


def test_start_scripts_leave_busy_stack_running_and_check_early_exits() -> None:
    for relative_path in (
        "bridge/windows/start-dual-tab.ps1",
        "standalone/web2api-chrome/start-dual-tab.ps1",
    ):
        source = _source(relative_path)
        assert "$busy =" in source
        assert "active_asset_transfers -gt 0" in source
        assert "$existingHealth.busy -eq $true" in source
        assert "$knownUnhealthy = $true" in source
        assert "$allRuntimePortsOpen -and -not $knownUnhealthy" in source
        assert "$stableBackends" in source
        assert "handling or waiting on a request" in source
        assert source.index("if ($busy -or $stable)") < source.index(
            "$existing = Get-CimInstance"
        )
        assert "$worker1.HasExited" in source
        assert "$worker2.HasExited" in source
        assert "$gateway.HasExited" in source
        assert "Local API port $port is still in use" in source


def test_manual_start_only_unpauses_watchdog_after_success() -> None:
    source = _source("standalone/web2api-chrome/start-dual-tab.ps1")
    first_pause_removal = source.index(
        "Remove-Item -LiteralPath $PauseFile -Force -ErrorAction SilentlyContinue"
    )
    mutex_acquired = source.index("$hasStartMutex = $startMutex.WaitOne(0)")
    assert mutex_acquired < first_pause_removal
    assert "[switch]$FromWatchdog" in source


def test_stop_and_background_scripts_cover_browser_and_watchdog_lifecycle() -> None:
    for relative_path in (
        "bridge/windows/stop-dual-tab.ps1",
        "standalone/web2api-chrome/stop-dual-tab.ps1",
    ):
        source = _source(relative_path)
        assert "--remote-debugging-port(?:=|\\s+)9325" in source
        assert "foreach ($process in $browserTargets)" in source

    installer = _source("standalone/web2api-chrome/install-background.ps1")
    assert "Get-ExistingWatchdogProcesses" in installer
    assert "Stop-Process -Id $watchdog.ProcessId -Force" in installer
    assert "'-NoProfile', '-STA', '-WindowStyle', 'Hidden'" in installer


def test_stale_upload_cleanup_keeps_recent_pair_and_completed_files(tmp_path) -> None:
    module = _load_upload_cleanup_module()
    staging = tmp_path / "user" / "session" / "uploads" / ".upload-staging"
    staging.mkdir(parents=True)
    completed = staging.parent / "completed.txt"
    completed.write_text("complete", encoding="utf-8")

    stale_id = "a" * 32
    recent_id = "b" * 32
    stale_paths = [staging / f"{stale_id}.part", staging / f"{stale_id}.json"]
    recent_paths = [staging / f"{recent_id}.part", staging / f"{recent_id}.json"]
    for candidate in stale_paths + recent_paths:
        candidate.write_text("staged", encoding="utf-8")

    now = 200_000.0
    for candidate in stale_paths + [recent_paths[1]]:
        os.utime(candidate, (now - 100_000, now - 100_000))
    os.utime(recent_paths[0], (now - 10, now - 10))

    removed, failures = module.cleanup(tmp_path, 86_400, now=now)

    assert (removed, failures) == (2, 0)
    assert not any(candidate.exists() for candidate in stale_paths)
    assert all(candidate.exists() for candidate in recent_paths)
    assert completed.read_text(encoding="utf-8") == "complete"


def test_install_and_update_enable_upload_cleanup_timer() -> None:
    for relative_path in ("scripts/install-server.sh", "scripts/update-server.sh"):
        source = _source(relative_path)
        assert "cleanup-stale-uploads.py" in source
        assert "ai-chat-upload-cleanup.service" in source
        assert "ai-chat-upload-cleanup.timer" in source
        assert "systemctl enable --now ai-chat-upload-cleanup.timer" in source

    timer = _source("deploy/systemd/ai-chat-upload-cleanup.timer")
    service = _source("deploy/systemd/ai-chat-upload-cleanup.service")
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "--max-age-seconds 86400" in service
    assert "User=ai-chat" in service


def test_update_restores_running_services_on_error_exit_or_signal() -> None:
    source = _source("scripts/update-server.sh")
    assert "trap restore_website_on_error ERR EXIT" in source
    assert "trap 'exit 130' INT" in source
    assert "trap 'exit 143' TERM" in source
    assert "systemctl start ai-chat-image-bridge.service || true" in source
    assert "systemctl start ai-chat.service || true" in source


def test_update_snapshots_code_before_mutation_and_restores_on_failure() -> None:
    source = _source("scripts/update-server.sh")
    create = source.index('deployment-snapshot.py" create')
    stop = source.index("systemctl stop ai-chat.service", create)
    mutated = source.index("deployment_mutated=true", stop)
    first_rsync = source.index("rsync -a --delete", mutated)
    assert create < stop < mutated < first_rsync
    handler = source[source.index("restore_website_on_error()") : source.index(
        "trap restore_website_on_error"
    )]
    assert "systemctl stop ai-chat.service || true" in handler
    assert "systemctl stop ai-chat-image-bridge.service || true" in handler
    assert 'deployment-snapshot.py" restore' in handler
    assert handler.index("systemctl stop ai-chat.service || true") < handler.index(
        'deployment-snapshot.py" restore'
    )
    assert "snapshot_restored=true" in handler
    assert 'deployment-snapshot.py" discard' in source
    assert source.index('deployment-snapshot.py" discard', first_rsync) > source.index(
        "systemctl reload nginx", first_rsync
    )


def test_deployment_snapshot_cli_restores_only_replaceable_code(tmp_path) -> None:
    tool = PROJECT_ROOT / "scripts/deployment-snapshot.py"
    install_root = tmp_path / "ai-chat"
    components = {
        "website/version.txt": "old-website",
        "services/image-bridge/version.txt": "old-bridge",
        "scripts/version.txt": "old-scripts",
    }
    for relative_path, content in components.items():
        target = install_root / "current" / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    persistent = {
        "data/accounts.sqlite3": "account-data",
        "workspaces/user/session.txt": "workspace-data",
    }
    for relative_path, content in persistent.items():
        target = install_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    created = subprocess.run(
        [
            sys.executable,
            str(tool),
            "create",
            "--install-root",
            str(install_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot = Path(created.stdout.strip())
    assert snapshot.parent == install_root / "state"

    for relative_path in components:
        (install_root / "current" / relative_path).write_text(
            "partial-update", encoding="utf-8"
        )
    extra = install_root / "current/website/new-partial-file.txt"
    extra.write_text("new", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(tool),
            "restore",
            "--install-root",
            str(install_root),
            "--snapshot",
            str(snapshot),
        ],
        check=True,
    )

    for relative_path, content in components.items():
        assert (install_root / "current" / relative_path).read_text(
            encoding="utf-8"
        ) == content
    assert not extra.exists()
    for relative_path, content in persistent.items():
        assert (install_root / relative_path).read_text(encoding="utf-8") == content

    subprocess.run(
        [
            sys.executable,
            str(tool),
            "discard",
            "--install-root",
            str(install_root),
            "--snapshot",
            str(snapshot),
        ],
        check=True,
    )
    assert not snapshot.exists()


def test_deployment_snapshot_refuses_cleanup_outside_controlled_state(tmp_path) -> None:
    tool = PROJECT_ROOT / "scripts/deployment-snapshot.py"
    install_root = tmp_path / "ai-chat"
    (install_root / "state").mkdir(parents=True)
    outside = tmp_path / "update-backup-outside"
    outside.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(tool),
            "discard",
            "--install-root",
            str(install_root),
            "--snapshot",
            str(outside),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert outside.is_dir()
    assert "under state" in result.stderr
