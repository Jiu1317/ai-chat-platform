"""Configuration model and strict JSON loader."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .exceptions import ConfigurationError

DEEPSEEK_URL = "https://chat.deepseek.com/"
DEFAULT_MODELS = ("deepseek-chat", "deepseek-reasoner")


def _default_profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "DeepSeek-Web2API" / "ChromeProfile"
    return Path.home() / ".deepseek-web2api" / "chrome-profile"


@dataclass(frozen=True, slots=True)
class ChromeConfig:
    """Dedicated Chrome instance configuration.

    ``user_data_dir`` belongs exclusively to this bridge.  Chrome manages the
    login state inside it; Python code never opens or copies its cookie files.
    """

    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9332
    user_data_dir: Path = field(default_factory=_default_profile_dir)
    binary: str | None = None
    auto_launch: bool = True
    startup_timeout_seconds: float = 30.0
    page_url: str = DEEPSEEK_URL


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 9191
    api_keys: tuple[str, ...] = ()
    request_timeout_seconds: float = 600.0
    poll_interval_seconds: float = 0.25
    stable_polls: int = 3
    max_request_bytes: int = 30 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    default_model: str = "deepseek-chat"
    models: tuple[str, ...] = DEFAULT_MODELS


@dataclass(frozen=True, slots=True)
class Config:
    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)

    def with_overrides(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        cdp_port: int | None = None,
        profile_dir: str | Path | None = None,
    ) -> Config:
        server = replace(
            self.server,
            host=self.server.host if host is None else host,
            port=self.server.port if port is None else port,
        )
        chrome = replace(
            self.chrome,
            cdp_port=self.chrome.cdp_port if cdp_port is None else cdp_port,
            user_data_dir=(
                self.chrome.user_data_dir
                if profile_dir is None
                else _expand_path(profile_dir)
            ),
        )
        result = replace(self, chrome=chrome, server=server)
        _validate(result)
        return result


def _expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _mapping(value: Any, section: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration section '{section}' must be an object")
    return value


def _unknown_keys(data: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigurationError(
            f"Unknown key(s) in '{section}': {', '.join(unknown)}"
        )


def _tuple_of_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"'{field_name}' must be a JSON array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"'{field_name}' must be a string")
    return value.strip()


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"'{field_name}' must be an integer")
    return value


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"'{field_name}' must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"'{field_name}' must be a finite number")
    return result


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"'{field_name}' must be true or false")
    return value


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from JSON without creating or mutating any files."""

    raw: dict[str, Any] = {}
    if path is not None:
        config_path = _expand_path(path)
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Configuration file not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"Invalid JSON in configuration file at line {exc.lineno}, column {exc.colno}"
            ) from exc
        except OSError as exc:
            raise ConfigurationError(f"Could not read configuration file: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError("Configuration root must be a JSON object")
        raw = loaded

    _unknown_keys(raw, {"chrome", "server", "deepseek"}, "root")
    chrome_raw = _mapping(raw.get("chrome"), "chrome")
    server_raw = _mapping(raw.get("server"), "server")
    deepseek_raw = _mapping(raw.get("deepseek"), "deepseek")

    _unknown_keys(
        chrome_raw,
        {
            "cdp_host",
            "cdp_port",
            "user_data_dir",
            "profile_dir",
            "binary",
            "auto_launch",
            "startup_timeout_seconds",
            "page_url",
        },
        "chrome",
    )
    _unknown_keys(
        server_raw,
        {
            "host",
            "port",
            "api_keys",
            "request_timeout_seconds",
            "poll_interval_seconds",
            "stable_polls",
            "max_request_bytes",
        },
        "server",
    )
    _unknown_keys(deepseek_raw, {"default_model", "models"}, "deepseek")

    default_chrome = ChromeConfig()
    profile_value = chrome_raw.get(
        "user_data_dir", chrome_raw.get("profile_dir", default_chrome.user_data_dir)
    )
    if not isinstance(profile_value, (str, Path)):
        raise ConfigurationError("'chrome.user_data_dir' must be a string")
    binary_value = chrome_raw.get("binary")
    chrome = ChromeConfig(
        cdp_host=_string(
            chrome_raw.get("cdp_host", default_chrome.cdp_host), "chrome.cdp_host"
        ),
        cdp_port=_integer(
            chrome_raw.get("cdp_port", default_chrome.cdp_port), "chrome.cdp_port"
        ),
        user_data_dir=_expand_path(profile_value),
        binary=(
            _string(binary_value, "chrome.binary") if binary_value is not None else None
        ),
        auto_launch=_boolean(
            chrome_raw.get("auto_launch", default_chrome.auto_launch),
            "chrome.auto_launch",
        ),
        startup_timeout_seconds=_number(
            chrome_raw.get(
                "startup_timeout_seconds", default_chrome.startup_timeout_seconds
            ),
            "chrome.startup_timeout_seconds",
        ),
        page_url=_string(
            chrome_raw.get("page_url", default_chrome.page_url), "chrome.page_url"
        ),
    )

    default_server = ServerConfig()
    server = ServerConfig(
        host=_string(server_raw.get("host", default_server.host), "server.host"),
        port=_integer(server_raw.get("port", default_server.port), "server.port"),
        api_keys=_tuple_of_strings(server_raw.get("api_keys"), "server.api_keys"),
        request_timeout_seconds=_number(
            server_raw.get(
                "request_timeout_seconds", default_server.request_timeout_seconds
            ),
            "server.request_timeout_seconds",
        ),
        poll_interval_seconds=_number(
            server_raw.get(
                "poll_interval_seconds", default_server.poll_interval_seconds
            ),
            "server.poll_interval_seconds",
        ),
        stable_polls=_integer(
            server_raw.get("stable_polls", default_server.stable_polls),
            "server.stable_polls",
        ),
        max_request_bytes=_integer(
            server_raw.get("max_request_bytes", default_server.max_request_bytes),
            "server.max_request_bytes",
        ),
    )

    default_deepseek = DeepSeekConfig()
    models = (
        _tuple_of_strings(deepseek_raw.get("models"), "deepseek.models")
        if "models" in deepseek_raw
        else default_deepseek.models
    )
    deepseek = DeepSeekConfig(
        default_model=_string(
            deepseek_raw.get("default_model", default_deepseek.default_model),
            "deepseek.default_model",
        ),
        models=models,
    )

    result = Config(chrome=chrome, server=server, deepseek=deepseek)
    _validate(result)
    return result


def _validate(config: Config) -> None:
    chrome = config.chrome
    server = config.server
    deepseek = config.deepseek

    for field_name, port in (
        ("chrome.cdp_port", chrome.cdp_port),
        ("server.port", server.port),
    ):
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigurationError(f"'{field_name}' must be an integer")
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"'{field_name}' must be between 1 and 65535")
    if not chrome.cdp_host or not server.host:
        raise ConfigurationError("Host names cannot be empty")
    if not math.isfinite(chrome.startup_timeout_seconds):
        raise ConfigurationError("'chrome.startup_timeout_seconds' must be finite")
    if chrome.startup_timeout_seconds <= 0:
        raise ConfigurationError("'chrome.startup_timeout_seconds' must be positive")
    page = urlparse(chrome.page_url)
    if page.scheme != "https" or page.hostname != "chat.deepseek.com":
        raise ConfigurationError("'chrome.page_url' must be the official DeepSeek chat URL")
    if not math.isfinite(server.request_timeout_seconds):
        raise ConfigurationError("'server.request_timeout_seconds' must be finite")
    if server.request_timeout_seconds <= 0:
        raise ConfigurationError("'server.request_timeout_seconds' must be positive")
    if not 0.05 <= server.poll_interval_seconds <= 5:
        raise ConfigurationError("'server.poll_interval_seconds' must be between 0.05 and 5")
    if not 2 <= server.stable_polls <= 20:
        raise ConfigurationError("'server.stable_polls' must be between 2 and 20")
    if not 1024 <= server.max_request_bytes <= 64 * 1024 * 1024:
        raise ConfigurationError("'server.max_request_bytes' must be between 1 KiB and 64 MiB")
    if len(set(server.api_keys)) != len(server.api_keys):
        raise ConfigurationError("'server.api_keys' cannot contain duplicates")
    if not deepseek.models:
        raise ConfigurationError("'deepseek.models' cannot be empty")
    if not set(deepseek.models).issubset(DEFAULT_MODELS):
        raise ConfigurationError(
            "'deepseek.models' may contain only deepseek-chat and deepseek-reasoner"
        )
    if deepseek.default_model not in deepseek.models:
        raise ConfigurationError("'deepseek.default_model' must be listed in 'deepseek.models'")
