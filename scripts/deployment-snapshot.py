#!/usr/bin/env python3
"""Create, restore, and discard tightly scoped AI Chat deployment snapshots."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

COMPONENTS = ("website", "services/image-bridge", "scripts")
BACKUP_PREFIX = "update-backup-"
MANIFEST_NAME = "manifest.json"


class SnapshotError(RuntimeError):
    """Raised when a deployment snapshot path or payload is invalid."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _install_root(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == resolved.parent:
        raise SnapshotError("install root must not be a filesystem root")
    return resolved


def _component_target(root: Path, component: str) -> Path:
    if component not in COMPONENTS:
        raise SnapshotError(f"unsupported deployment component: {component}")
    current = (root / "current").resolve()
    target = current.joinpath(*component.split("/"))
    resolved = target.resolve(strict=False)
    if not _is_within(resolved, current) or resolved == current:
        raise SnapshotError(f"component escapes current deployment: {component}")
    return target


def _validated_snapshot(root: Path, snapshot: Path, *, require_manifest: bool) -> Path:
    state = (root / "state").resolve()
    resolved = snapshot.resolve()
    if resolved.parent != state or not resolved.name.startswith(BACKUP_PREFIX):
        raise SnapshotError("snapshot must be an update-backup-* directory under state")
    if require_manifest and not (resolved / MANIFEST_NAME).is_file():
        raise SnapshotError("snapshot manifest is missing")
    return resolved


def _remove_snapshot(root: Path, snapshot: Path, *, require_manifest: bool) -> None:
    controlled = _validated_snapshot(root, snapshot, require_manifest=require_manifest)
    shutil.rmtree(controlled)


def _remove_component(root: Path, component: str) -> None:
    target = _component_target(root, component)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def _remove_transient(target: Path, candidate: Path, prefix: str) -> None:
    if candidate.parent.resolve() != target.parent.resolve() or not candidate.name.startswith(
        prefix
    ):
        raise SnapshotError(f"refusing to remove uncontrolled path: {candidate}")
    if candidate.is_dir() and not candidate.is_symlink():
        shutil.rmtree(candidate)
    elif candidate.exists() or candidate.is_symlink():
        candidate.unlink()


def create_snapshot(install_root: Path) -> Path:
    root = _install_root(install_root)
    state = root / "state"
    state.mkdir(mode=0o750, parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=state)).resolve()
    manifest = {"version": 1, "components": {}}
    try:
        for component in COMPONENTS:
            target = _component_target(root, component)
            if target.exists() and not target.is_dir():
                raise SnapshotError(f"component is not a directory: {target}")
            existed = target.is_dir()
            manifest["components"][component] = {"existed": existed}
            if existed:
                destination = snapshot / "payload" / Path(component)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target, destination, symlinks=True)

        temporary = snapshot / f".{MANIFEST_NAME}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, snapshot / MANIFEST_NAME)
        return snapshot
    except BaseException:
        _remove_snapshot(root, snapshot, require_manifest=False)
        raise


def _load_manifest(snapshot: Path) -> dict[str, bool]:
    try:
        decoded = json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))
        components = decoded["components"]
        if decoded.get("version") != 1 or set(components) != set(COMPONENTS):
            raise ValueError
        existed_values = {
            component: components[component]["existed"] for component in COMPONENTS
        }
        if any(type(value) is not bool for value in existed_values.values()):
            raise ValueError
        return existed_values
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotError("snapshot manifest is invalid") from exc


def _restore_component(root: Path, snapshot: Path, component: str) -> None:
    source = snapshot / "payload" / Path(component)
    target = _component_target(root, component)
    target.parent.mkdir(parents=True, exist_ok=True)
    replacement = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
    )
    retired = target.parent / f".{target.name}.replaced-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, replacement, dirs_exist_ok=True, symlinks=True)
        if target.exists() or target.is_symlink():
            os.replace(target, retired)
        try:
            os.replace(replacement, target)
        except BaseException:
            if retired.exists() or retired.is_symlink():
                os.replace(retired, target)
            raise
        _remove_transient(target, retired, f".{target.name}.replaced-")
    finally:
        _remove_transient(target, replacement, f".{target.name}.restore-")


def restore_snapshot(install_root: Path, snapshot_path: Path) -> None:
    root = _install_root(install_root)
    snapshot = _validated_snapshot(root, snapshot_path, require_manifest=True)
    components = _load_manifest(snapshot)

    # Validate every required payload before replacing any live component.
    for component, existed in components.items():
        _component_target(root, component)
        payload = snapshot / "payload" / Path(component)
        if existed and (not payload.is_dir() or payload.is_symlink()):
            raise SnapshotError(f"snapshot payload is missing or invalid: {component}")

    for component, existed in components.items():
        if existed:
            _restore_component(root, snapshot, component)
        else:
            _remove_component(root, component)


def discard_snapshot(install_root: Path, snapshot_path: Path) -> None:
    root = _install_root(install_root)
    _remove_snapshot(root, snapshot_path, require_manifest=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "restore", "discard"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--install-root", required=True, type=Path)
        if command != "create":
            subparser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "create":
            print(create_snapshot(args.install_root))
        elif args.command == "restore":
            restore_snapshot(args.install_root, args.snapshot)
        else:
            discard_snapshot(args.install_root, args.snapshot)
    except (OSError, SnapshotError) as exc:
        print(f"deployment snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
