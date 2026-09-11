"""The `doctor` subcommand: discover broken functions + print diagnostic evidence.

doctor is the human/agent-facing surface for the assisted-fix workflow. It
AUTO-DISCovers broken functions from captured artifacts — no human needs to
name the function — and prints the evidence an AI repair agent reads to
propose a corrected payload/selector/parse. `doctor --verify <function>`
(Task 6) then re-runs the function live to confirm a fix.

The project ships capture + doctor; the actual fix-proposal is done by an
external AI agent pointed at the printed evidence, not a bundled model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Artifact filenames are <function>-<YYYYMMDD>-<HHMMSS>-<NNNNNN>.json. Anchor on
# the trailing timestamp+seq so function names containing dashes/underscores are
# preserved. Matches the format produced by DiagnosticsDir.capture.
_ARTIFACT_RE = re.compile(r"^(.+)-\d{8}-\d{6}-\d{6}$")


def list_broken_functions(base: Path) -> list[str]:
    """Return distinct function names that have at least one artifact.

    This is the auto-discovery entrypoint: instead of a human naming the broken
    function, the diagnostics dir self-reports which functions have captured
    breakage. Sorted for stable output.
    """
    base = Path(base)
    names = set()
    for p in base.glob("*.json"):
        m = _ARTIFACT_RE.match(p.stem)
        names.add(m.group(1) if m else p.stem.split("-")[0])
    return sorted(names)


def latest_artifact_for(base: Path, function: str) -> Path | None:
    """Return the most recent artifact path for *function*, or None."""
    files = sorted(Path(base).glob(f"{function}-*.json"))
    return files[-1] if files else None


def print_evidence(artifact_path: Path) -> None:
    """Print the captured diagnostic evidence for human / AI-agent reading.

    Output is plain text with clearly labeled sections so an AI repair agent
    can parse it. Mirrors the throwaway probe scripts used during debugging,
    but produced automatically and reproducibly.
    """
    data = json.loads(Path(artifact_path).read_text())
    print("=" * 60)
    print(f"FUNCTION:   {data.get('function', '?')}")
    print(f"TIMESTAMP:  {data.get('timestamp', '?')}")
    print(f"MISMATCH:   {data.get('mismatch', '?')}")
    print("=" * 60)
    print("\n--- REQUEST (what was sent) ---")
    print(json.dumps(data.get("request", {}), indent=2, default=str)[:2000])
    print("\n--- RESPONSE (what came back) ---")
    print(json.dumps(data.get("response", {}), indent=2, default=str)[:2000])
    print("\n--- EXPECTED shape ---")
    print(json.dumps(data.get("expected", {}), indent=2, default=str))
    print("\n--- ACTUAL ---")
    print(json.dumps(data.get("actual", {}), indent=2, default=str)[:2000])
    print("\nNext: an AI agent reads the above and proposes a corrected "
          "payload/selector/parse. Run `doctor --verify <function>` to test it.")


def run_doctor(args) -> None:
    """Entry point for the `doctor` subcommand."""
    from .diagnostics import _DIAG_DIR
    base = _DIAG_DIR.base

    if getattr(args, "verify", None):
        from .doctor_verify import run_verify_cli
        run_verify_cli(args.verify)
        return

    function = getattr(args, "function", None)

    # No function given → auto-discover and report all broken functions.
    if not function:
        fns = list_broken_functions(base)
        if not fns:
            print("No diagnostic artifacts found. To capture breakage, run the "
                  "server with W2A_DIAGNOSE=1 and trigger the failing function.")
            return
        print(f"Discovered {len(fns)} function(s) with captured breakage:")
        for fn in fns:
            print(f"  {fn}")
        print("\nRun `doctor <function>` to see the evidence, or "
              "`doctor --verify <function>` to re-test a fix.")
        return

    latest = latest_artifact_for(base, function)
    if latest is None:
        print(f"No artifact for '{function}'. Capture one by enabling "
              "W2A_DIAGNOSE=1 and triggering the breakage.")
        return
    print_evidence(latest)
