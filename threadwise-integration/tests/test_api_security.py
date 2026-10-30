from __future__ import annotations

from fastapi.testclient import TestClient

from threadwise.api import create_app
from threadwise.config import Settings
from threadwise.database import Database
from threadwise.service import ThreadwiseService


class FakeMattermost:
    def get_me(self) -> dict:
        return {"id": "b" * 26}

    def close(self) -> None:
        pass


class FakeMemory:
    pass


class FakeAgents:
    pass


def make_test_app():
    settings = Settings(
        mattermost_url="http://localhost",
        mattermost_token="bot-secret",
        slash_command_token="",
        mem0_api_key="mem0-secret",
        groq_api_key="groq-secret",
        database_path=":memory:",
        fast_model="fast",
        summary_model="large",
        commitment_days=3,
        inactivity_days=14,
        digest_time="09:00",
        timezone="UTC",
        max_thread_posts=100,
        enable_websocket=False,
        enable_digests=False,
        summarize_slash_token="summarize-secret",
        email_slash_token="email-secret",
        threadwise_slash_token="threadwise-secret",
    )
    db = Database(":memory:")
    service = ThreadwiseService(settings, FakeMattermost(), FakeMemory(), FakeAgents(), db)
    return create_app(settings, service)


def test_slash_command_rejects_bad_token() -> None:
    with TestClient(make_test_app()) as client:
        response = client.post(
            "/hooks/threadwise",
            headers={"Authorization": "Token wrong"},
            data={"command": "/threadwise", "user_id": "u" * 26, "text": "digest on"},
        )
    assert response.status_code == 200
    assert "not authorized" in response.json()["text"]


def test_each_slash_command_accepts_its_own_token() -> None:
    app = make_test_app()
    app.state.service.summarize_email = lambda user_id, channel_id, text: (
        "Email summary",
        "draft-id",
        None,
    )
    routes = (
        ("/hooks/summarize", "/summarize", "summarize-secret"),
        ("/hooks/threadwise-email", "/threadwise-email", "email-secret"),
        ("/hooks/threadwise", "/threadwise", "threadwise-secret"),
    )

    with TestClient(app) as client:
        for path, command, token in routes:
            response = client.post(
                path,
                headers={"Authorization": f"Token {token}"},
                data={
                    "command": command,
                    "user_id": "u" * 26,
                    "channel_id": "c" * 26,
                    "text": "email text" if command == "/threadwise-email" else "digest on",
                },
            )
            assert "not authorized" not in response.json()["text"]


def test_slash_command_tokens_are_isolated_by_command(monkeypatch) -> None:
    monkeypatch.setenv("MATTERMOST_SLASH_TOKEN", "")
    monkeypatch.setenv("MATTERMOST_SUMMARIZE_SLASH_TOKEN", "summarize-secret")
    monkeypatch.setenv("MATTERMOST_EMAIL_SLASH_TOKEN", "email-secret")
    monkeypatch.setenv("MATTERMOST_THREADWISE_SLASH_TOKEN", "threadwise-secret")
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("MEM0_API_KEY", "mem0-secret")
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    settings = Settings.from_env()
    app = create_app(
        settings,
        ThreadwiseService(
            settings, FakeMattermost(), FakeMemory(), FakeAgents(), Database(":memory:")
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/hooks/threadwise",
            headers={"Authorization": "Token email-secret"},
            data={"command": "/threadwise", "user_id": "u" * 26, "text": "digest on"},
        )

    assert "not authorized" in response.json()["text"]


def test_legacy_shared_slash_token_remains_supported(monkeypatch) -> None:
    monkeypatch.setenv("MATTERMOST_SLASH_TOKEN", "shared-secret")
    for name in (
        "MATTERMOST_SUMMARIZE_SLASH_TOKEN",
        "MATTERMOST_EMAIL_SLASH_TOKEN",
        "MATTERMOST_THREADWISE_SLASH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert all(
        settings.slash_token_for(command) == "shared-secret"
        for command in ("/summarize", "/threadwise-email", "/threadwise")
    )


def test_health_route() -> None:
    with TestClient(make_test_app()) as client:
        response = client.get("/health")
    assert response.json() == {"status": "ok"}
