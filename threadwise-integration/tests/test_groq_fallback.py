from __future__ import annotations

import pytest

from threadwise.ai import GroqAgents, GroqQuotaExceeded
from threadwise.config import Settings
from threadwise.schemas import ThreadSummary


class RateLimitError(Exception):
    status_code = 429


def settings() -> Settings:
    return Settings(
        mattermost_url="http://localhost",
        mattermost_token="bot-secret",
        slash_command_token="slash-secret",
        mem0_api_key="mem0-secret",
        groq_api_key="groq-secret",
        database_path=":memory:",
        fast_model="fast-model",
        summary_model="large-model",
        commitment_days=3,
        inactivity_days=14,
        digest_time="09:00",
        timezone="UTC",
        max_thread_posts=100,
        enable_websocket=False,
        enable_digests=False,
    )


def test_summary_uses_fast_model_after_large_model_rate_limit(monkeypatch) -> None:
    models: list[str] = []

    class FakeGroq:
        def __init__(self, *, model: str, **kwargs) -> None:
            self.model = model
            models.append(model)

    class FakeAgent:
        def __init__(self, model: FakeGroq) -> None:
            self.model = model

        def invoke(self, *args, **kwargs):
            if self.model.model == "large-model":
                raise RateLimitError()
            return {"structured_response": ThreadSummary(tldr="A concise summary.")}

    def create_agent(*, model, **kwargs):
        return FakeAgent(model)

    monkeypatch.setattr("langchain.agents.create_agent", create_agent)
    monkeypatch.setattr("langchain_groq.ChatGroq", FakeGroq)

    result = GroqAgents(settings()).summarize([], lambda: "verified thread")

    assert result.tldr == "A concise summary."
    assert models == ["large-model", "fast-model"]


def test_exhausted_fast_model_returns_explicit_quota_error(monkeypatch) -> None:
    class FakeGroq:
        def __init__(self, *, model: str, **kwargs) -> None:
            self.model = model

    class FakeAgent:
        def invoke(self, *args, **kwargs):
            raise RateLimitError()

    monkeypatch.setattr("langchain.agents.create_agent", lambda **kwargs: FakeAgent())
    monkeypatch.setattr("langchain_groq.ChatGroq", FakeGroq)

    with pytest.raises(GroqQuotaExceeded, match="quota"):
        GroqAgents(settings()).answer("question", [], lambda: "verified thread")
