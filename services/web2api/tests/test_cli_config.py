"""Regression tests for CLI/config precedence."""

import argparse

from chatgpt_web2api.__main__ import _add_common_args


def test_omitted_log_level_preserves_config_value():
    parser = argparse.ArgumentParser()
    _add_common_args(parser)

    args = parser.parse_args([])

    assert args.log_level is None


def test_explicit_log_level_is_still_available():
    parser = argparse.ArgumentParser()
    _add_common_args(parser)

    args = parser.parse_args(["--log-level", "DEBUG"])

    assert args.log_level == "DEBUG"
