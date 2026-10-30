from __future__ import annotations

from threadwise.config import Settings
from threadwise.database import Database
from threadwise.service import ThreadwiseService


class FakeMattermost:
    def __init__(self) -> None:
        self.posts = [
            {
                "id": "a" * 26,
                "root_id": "",
                "channel_id": "c" * 26,
                "user_id": "u" * 26,
                "create_at": 1000,
                "message": "Client asked about pricing.",
            }
        ]

    def user_in_channel(self, channel_id: str, user_id: str) -> bool:
        return True

    def get_thread(self, post_id: str, per_page: int = 100) -> list[dict]:
        return self.posts

    def get_channel(self, channel_id: str) -> dict:
        return {"id": channel_id, "name": "sales"}

    def get_user(self, user_id: str) -> dict:
        return {"id": user_id, "username": "alex"}


class FakeMemory:
    def __init__(self) -> None:
        self.searched: list[dict] = []
        self.writes: list[dict] = []

    def search(self, **kwargs) -> list[str]:
        self.searched.append(kwargs)
        return []

    def add(self, **kwargs) -> None:
        self.writes.append(kwargs)

    @staticmethod
    def metadata(**kwargs) -> dict:
        return kwargs


class FakeAgents:
    def __init__(self) -> None:
        self.summarize_calls = 0

    def summarize(self, memories, read_context, email=False):
        from threadwise.schemas import ThreadSummary

        self.summarize_calls += 1
        assert "Client asked about pricing." in read_context()
        return ThreadSummary(tldr="The client asked about pricing.", entity_name="Client A")


def settings() -> Settings:
    return Settings(
        mattermost_url="http://localhost",
        mattermost_token="bot-secret",
        slash_command_token="slash-secret",
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
    )


def test_summary_cache_is_user_scoped_and_invalidates_on_new_post() -> None:
    mm = FakeMattermost()
    memory = FakeMemory()
    agents = FakeAgents()
    db = Database(":memory:")
    service = ThreadwiseService(settings(), mm, memory, agents, db)

    service.summarize("a" * 26, "c" * 26, "u" * 26)
    service.summarize("a" * 26, "c" * 26, "u" * 26)
    assert agents.summarize_calls == 1
    assert memory.searched[0]["user_id"] == "u" * 26

    service.summarize("a" * 26, "c" * 26, "v" * 26)
    assert agents.summarize_calls == 2
    assert memory.searched[-1]["user_id"] == "v" * 26

    mm.posts.append(
        {
            "id": "b" * 26,
            "root_id": "a" * 26,
            "channel_id": "c" * 26,
            "user_id": "u" * 26,
            "create_at": 2000,
            "message": "We will send a quote tomorrow.",
        }
    )
    service.summarize("a" * 26, "c" * 26, "u" * 26)
    assert agents.summarize_calls == 3


def test_digest_only_surfaces_owned_unfollowed_commitments() -> None:
    db = Database(":memory:")
    detected_at = 1_000
    thread_id = "a" * 26
    channel_id = "c" * 26
    db.record_activity(
        {
            "id": thread_id,
            "root_id": "",
            "channel_id": channel_id,
            "create_at": detected_at,
        },
        "sales",
    )
    db.save_summary(
        user_id="requester",
        channel_id=channel_id,
        thread_id=thread_id,
        latest_post_id=thread_id,
        summary={
            "tldr": "Quote promised.",
            "open_followups": [{"action": "Send the quote", "owner": "Alex"}],
        },
        channel_name="sales",
        owner_ids={"alex": "requester"},
        detected_at=detected_at,
    )

    overdue = db.digest_items(
        "requester",
        now_ms=detected_at + 4 * 86_400_000,
        commitment_days=3,
        inactivity_days=14,
    )
    assert len(overdue) == 1
    assert "Send the quote" in overdue[0]["text"]
    assert overdue[0]["channel_id"] == channel_id

    db.record_activity(
        {
            "id": "b" * 26,
            "root_id": thread_id,
            "channel_id": channel_id,
            "create_at": detected_at + 1,
        },
        "sales",
    )
    followed_up = db.digest_items(
        "requester",
        now_ms=detected_at + 4 * 86_400_000,
        commitment_days=3,
        inactivity_days=14,
    )
    assert not any("Send the quote" in item["text"] for item in followed_up)
