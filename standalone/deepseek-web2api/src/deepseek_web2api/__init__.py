"""Local DeepSeek web-to-API bridge.

This package controls only a dedicated Chrome debugging profile.  It never
reads, exports, or writes browser cookies itself.
"""

from .api_server import APIServer, create_app
from .cdp_driver import CDPDriver
from .config import Config, load_config

__all__ = ["APIServer", "CDPDriver", "Config", "create_app", "load_config"]
__version__ = "0.1.0"
