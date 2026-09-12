"""Keep the Windows standalone bundle on the canonical Web2API runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_PACKAGE = PROJECT_ROOT / "services" / "web2api"
EMBEDDED_PACKAGE = PROJECT_ROOT / "standalone" / "web2api-chrome" / "web2api"


def _normalized_sha256(path: Path) -> str:
    """Hash text content while ignoring checkout-specific newline style."""
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def _mirrored_files() -> list[Path]:
    package_files = [
        path.relative_to(CANONICAL_PACKAGE)
        for path in (CANONICAL_PACKAGE / "src" / "chatgpt_web2api").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".md"}
    ]
    return [
        Path("LICENSE"),
        Path("README.md"),
        Path("config.example.json"),
        Path("pyproject.toml"),
        *sorted(package_files),
    ]


def test_standalone_embedded_web2api_matches_canonical_package():
    mismatches = {}
    for relative_path in _mirrored_files():
        canonical = CANONICAL_PACKAGE / relative_path
        embedded = EMBEDDED_PACKAGE / relative_path
        if not embedded.is_file():
            mismatches[str(relative_path)] = "missing from standalone package"
            continue
        canonical_digest = _normalized_sha256(canonical)
        embedded_digest = _normalized_sha256(embedded)
        if canonical_digest != embedded_digest:
            mismatches[str(relative_path)] = (
                f"canonical={canonical_digest} standalone={embedded_digest}"
            )

    assert mismatches == {}, (
        "standalone/web2api-chrome/web2api drifted from services/web2api: "
        f"{mismatches}"
    )


def test_windows_setup_installs_the_embedded_package():
    setup = (
        PROJECT_ROOT / "standalone" / "web2api-chrome" / "setup-windows.ps1"
    ).read_text(encoding="utf-8")

    assert "$Web2ApiRoot = Join-Path $WorkRoot 'web2api'" in setup
    assert "& $VenvPython -m pip install -e $Web2ApiRoot" in setup
