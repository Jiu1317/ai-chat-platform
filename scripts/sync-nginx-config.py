#!/usr/bin/env python3
"""Synchronize AI Chat routes without overwriting Certbot/TLS directives."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

APP_MARKER = "proxy_pass http://127.0.0.1:13002"
MANAGED_LOCATION_HEADERS = {
    "location ^~ /v1/",
    "location /static/",
    "location = /healthz",
    "location /api/turn",
    "location = /api/uploads",
    "location ^~ /api/uploads/",
    "location /api/files/",
    "location ^~ /api/transfer-image/",
    "location /",
}


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("Nginx block is missing a closing brace")


def _blocks(text: str, keyword: str) -> list[tuple[int, int, str]]:
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(keyword)}\b[^{{\n]*\{{")
    blocks: list[tuple[int, int, str]] = []
    for match in pattern.finditer(text):
        opening = text.find("{", match.start(), match.end())
        closing = _matching_brace(text, opening)
        blocks.append((match.start(), closing + 1, text[match.start() : closing + 1]))
    return blocks


def _header(block: str) -> str:
    return " ".join(block.split("{", 1)[0].split())


def synchronize(template: str, installed: str) -> str:
    template_servers = [
        block for _start, _end, block in _blocks(template, "server")
        if APP_MARKER in block
    ]
    installed_servers = [
        item for item in _blocks(installed, "server")
        if APP_MARKER in item[2]
    ]
    if len(template_servers) != 1:
        raise ValueError("Template must contain exactly one AI Chat server block")
    if len(installed_servers) != 1:
        raise ValueError("Installed config must contain exactly one AI Chat server block")

    managed_locations = {
        _header(block): block.rstrip()
        for _start, _end, block in _blocks(template_servers[0], "location")
        if _header(block) in MANAGED_LOCATION_HEADERS
    }
    if set(managed_locations) != MANAGED_LOCATION_HEADERS:
        missing = ", ".join(sorted(MANAGED_LOCATION_HEADERS - set(managed_locations)))
        raise ValueError(f"Template is missing managed locations: {missing}")

    server_start, server_end, server = installed_servers[0]
    body = server
    removable = [
        (start, end)
        for start, end, block in _blocks(body, "location")
        if _header(block) in MANAGED_LOCATION_HEADERS
    ]
    for start, end in reversed(removable):
        while end < len(body) and body[end] in " \t":
            end += 1
        if end < len(body) and body[end] == "\r":
            end += 1
        if end < len(body) and body[end] == "\n":
            end += 1
        body = body[:start] + body[end:]

    direct_size = re.compile(r"(?m)^    client_max_body_size\s+[^;]+;[ \t]*\r?\n?")
    body = direct_size.sub("", body)
    closing = body.rfind("}")
    if closing < 0:
        raise ValueError("Installed AI Chat server block is invalid")
    newline = "\r\n" if "\r\n" in installed else "\n"
    insertion = (
        f"    client_max_body_size 55m;{newline}{newline}"
        + (newline + newline).join(
            managed_locations[header].replace("\n", newline)
            for header in (
                "location ^~ /v1/",
                "location /static/",
                "location = /healthz",
                "location /api/turn",
                "location = /api/uploads",
                "location ^~ /api/uploads/",
                "location /api/files/",
                "location ^~ /api/transfer-image/",
                "location /",
            )
        )
        + newline
    )
    body = body[:closing].rstrip() + newline + newline + insertion + "}"
    return installed[:server_start] + body + installed[server_end:]


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: sync-nginx-config.py TEMPLATE INSTALLED", file=sys.stderr)
        return 2
    template_path = Path(sys.argv[1])
    installed_path = Path(sys.argv[2])
    updated = synchronize(
        template_path.read_text(encoding="utf-8"),
        installed_path.read_text(encoding="utf-8"),
    )
    mode = installed_path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{installed_path.name}.",
        dir=installed_path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, installed_path)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
