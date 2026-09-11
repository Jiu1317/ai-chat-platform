"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from aiohttp import web

from .api_server import create_app
from .config import load_config
from .exceptions import ConfigurationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local DeepSeek Web2API bridge")
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--host", help="API listen host override")
    parser.add_argument("--port", type=int, help="API listen port override")
    parser.add_argument("--cdp-port", type=int, help="dedicated Chrome CDP port override")
    parser.add_argument("--profile-dir", type=Path, help="dedicated Chrome profile override")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        config = load_config(args.config).with_overrides(
            host=args.host,
            port=args.port,
            cdp_port=args.cdp_port,
            profile_dir=args.profile_dir,
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
        return
    web.run_app(create_app(config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
