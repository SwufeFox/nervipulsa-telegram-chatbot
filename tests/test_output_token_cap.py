from __future__ import annotations

import asyncio

from app.config import load_config
from app.main import build_settings
from app.agent import NervipulsaAgent
from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse
from nervipulsa.providers import ScriptedBackend


def test_app_output_cap_defaults_and_can_be_overridden(tmp_path):
    config = load_config(tmp_path / "missing.json", environ={})
    # Effectively unlimited by default: the cap was the binding constraint, not
    # the model (docs/LONG_CONVERSATION.md).
    assert config.max_output_tokens == 1_000_000
    assert config.context_limit == 1_000_000
    overridden = load_config(
        tmp_path / "missing.json", environ={"NERVIPULSA_MAX_OUTPUT_TOKENS": "8192"}
    )
    assert overridden.max_output_tokens == 8192
    assert build_settings(config).max_output_tokens == 1_000_000


def test_the_http_timeout_outlives_an_unlimited_reply(tmp_path):
    """A 1M-token reply cannot be fetched inside 60 seconds."""
    config = load_config(tmp_path / "missing.json", environ={})
    settings = build_settings(config)
    assert settings.max_timeout == config.request_timeout
    assert settings.max_timeout >= 600


def test_agent_passes_output_cap_to_model_request(tmp_path):
    backend = ScriptedBackend(lambda request: ModelResponse(content="ok"))
    agent = NervipulsaAgent(
        settings=Settings(model="scripted", max_output_tokens=1_000_000),
        workspace=tmp_path, system_prompt="Be helpful", backend=backend,
    )

    async def run():
        await agent.start()
        try:
            result = await agent.ask(chat_id=1, prompt="hello", tag="test")
            assert result.text == "ok"
        finally:
            await agent.stop()

    asyncio.run(run())
    assert backend.requests[0].max_output_tokens == 1_000_000
