from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.api_server import MODEL_MAP, APIServer
from chatgpt_web2api.cdp_driver import CDPDriver, ModelSelectionError


def test_current_chatgpt_model_aliases():
    assert MODEL_MAP["gpt-5.5"] == "gpt-5-5"
    assert MODEL_MAP["gpt-5.5-instant"] == "gpt-5-5-instant"
    assert MODEL_MAP["gpt-5.5-thinking"] == "gpt-5-5-thinking"
    assert MODEL_MAP["gpt-5.5-pro"] == "gpt-5-5-pro"

    assert MODEL_MAP["gpt-5.6"] == "gpt-5-6"
    assert MODEL_MAP["gpt-5.6-instant"] == "gpt-5-6-instant"
    assert MODEL_MAP["gpt-5.6-thinking"] == "gpt-5-6-thinking"
    assert MODEL_MAP["gpt-5.6-sol"] == "gpt-5.6-sol-wm"
    assert MODEL_MAP["gpt-5.6-pro"] == "gpt-5-6-pro"

    assert MODEL_MAP["gpt-6"] == "gpt-6-astra-wm"
    assert MODEL_MAP["gpt-6-astra"] == "gpt-6-astra-wm"
    assert MODEL_MAP["gpt-6-pro"] == "gpt-6-pro"


@pytest.mark.asyncio
async def test_model_selection_uses_live_account_catalog():
    driver = CDPDriver(cdp_port=9222)
    driver.get_models = AsyncMock(return_value=[{"slug": "gpt-5.6-sol-wm"}])
    driver._js = AsyncMock(side_effect=AssertionError("DOM picker should not be needed"))

    assert await driver.select_model("gpt-5.6-sol-wm") is True
    assert driver._current_model == "gpt-5.6-sol-wm"


@pytest.mark.asyncio
async def test_unavailable_model_fails_closed():
    driver = CDPDriver(cdp_port=9222)
    driver.get_models = AsyncMock(return_value=[{"slug": "gpt-5-5"}])

    assert await driver.select_model("gpt-6-does-not-exist") is False
    assert driver._current_model is None


def test_model_selection_error_is_openai_shaped_400():
    server = APIServer.__new__(APIServer)
    response = server._error_response(ModelSelectionError("missing-model"))
    assert response.status == 400
    assert b'"code": "model_not_available"' in response.body
