"""Typed failures returned by the local bridge."""

from __future__ import annotations


class DeepSeekWeb2APIError(Exception):
    """Base error with an API-safe code and HTTP status."""

    code = "bridge_error"
    http_status = 502


class ConfigurationError(DeepSeekWeb2APIError):
    code = "configuration_error"
    http_status = 500


class ChromeLaunchError(DeepSeekWeb2APIError):
    code = "chrome_launch_error"
    http_status = 503


class CDPConnectionError(DeepSeekWeb2APIError):
    code = "cdp_connection_error"
    http_status = 503


class CDPProtocolError(DeepSeekWeb2APIError):
    code = "cdp_protocol_error"
    http_status = 502


class DeepSeekTabNotFound(DeepSeekWeb2APIError):
    code = "deepseek_tab_not_found"
    http_status = 503


class AuthenticationRequired(DeepSeekWeb2APIError):
    code = "login_required"
    http_status = 401


class HumanVerificationRequired(DeepSeekWeb2APIError):
    """A challenge or risk-control page needs manual user action.

    The bridge intentionally has no method that attempts to solve or bypass it.
    """

    code = "captcha_required"
    http_status = 409


class DOMElementNotFound(DeepSeekWeb2APIError):
    code = "dom_changed"
    http_status = 503


class SendNotAcknowledged(DeepSeekWeb2APIError):
    """The click outcome is unknown; callers must never retry automatically."""

    code = "send_unknown"
    http_status = 502


class GenerationTimeout(DeepSeekWeb2APIError):
    code = "generation_timeout"
    http_status = 504


class GenerationCancelled(DeepSeekWeb2APIError):
    code = "generation_cancelled"
    http_status = 499


class GenerationRejected(DeepSeekWeb2APIError):
    code = "rate_limited"
    http_status = 429


class DeepSeekCompletionRejected(DeepSeekWeb2APIError):
    code = "generation_rejected"
    http_status = 502


class UnsupportedModel(DeepSeekWeb2APIError):
    code = "unsupported_model"
    http_status = 400


class InvalidRequest(DeepSeekWeb2APIError):
    code = "invalid_request"
    http_status = 400
