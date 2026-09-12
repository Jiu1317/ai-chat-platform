#!/usr/bin/env python3
"""Remove abandoned chunk-upload staging files without touching completed uploads."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

UPLOAD_FILE_RE = re.compile(r"([a-f0-9]{32})\.(?:part|json)\Z")
STAGING_DIR_NAME = ".upload-staging"


def iter_staging_directories(workspace_root: Path):
    for current, directories, _files in os.walk(workspace_root, followlinks=False):
        if STAGING_DIR_NAME in directories:
            staging = Path(current) / STAGING_DIR_NAME
            directories.remove(STAGING_DIR_NAME)
            yield staging


def cleanup_staging_directory(staging: Path, cutoff: float) -> tuple[int, int]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    unrecognized: list[Path] = []
    removed = 0
    failures = 0

    try:
        candidates = list(staging.iterdir())
    except OSError as exc:
        print(f"unable to inspect {staging}: {exc}", file=sys.stderr)
        return 0, 1

    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            matched = UPLOAD_FILE_RE.fullmatch(candidate.name)
            if matched:
                grouped[matched.group(1)].append(candidate)
            else:
                unrecognized.append(candidate)
        except OSError as exc:
            failures += 1
            print(f"unable to inspect {candidate}: {exc}", file=sys.stderr)

    groups = list(grouped.values()) + [[candidate] for candidate in unrecognized]
    for candidates in groups:
        try:
            # The .part file is touched by every received chunk. Keeping the
            # entire pair when either member is recent prevents an old JSON
            # receipt from being removed during a resumed upload.
            if any(candidate.stat().st_mtime >= cutoff for candidate in candidates):
                continue
            for candidate in candidates:
                # Recheck immediately before removal to narrow the race with a
                # chunk that resumed after this timer began scanning.
                if candidate.stat().st_mtime >= cutoff:
                    break
            else:
                for candidate in candidates:
                    candidate.unlink(missing_ok=True)
                    removed += 1
        except OSError as exc:
            failures += 1
            print(
                f"unable to remove stale upload group in {staging}: {exc}",
                file=sys.stderr,
            )

    return removed, failures


def cleanup(
    workspace_root: Path, max_age_seconds: int, *, now: float | None = None
) -> tuple[int, int]:
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    failures = 0
    if not workspace_root.is_dir():
        return 0, 0
    for staging in iter_staging_directories(workspace_root):
        staging_removed, staging_failures = cleanup_staging_directory(staging, cutoff)
        removed += staging_removed
        failures += staging_failures
    return removed, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace_root", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=24 * 60 * 60)
    args = parser.parse_args()
    if args.max_age_seconds < 60 * 60:
        parser.error("--max-age-seconds must be at least 3600")

    removed, failures = cleanup(args.workspace_root, args.max_age_seconds)
    print(f"stale upload cleanup: removed={removed} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
