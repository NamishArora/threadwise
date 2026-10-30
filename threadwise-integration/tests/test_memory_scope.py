from __future__ import annotations

from threadwise.memory import PREFERENCE_CHANNEL, PREFERENCE_THREAD, ScopedMemory


class FakeMem0Client:
    def __init__(self) -> None:
        self.filters = None

    def search(self, query: str, filters: dict) -> list[dict]:
        self.filters = filters
        return [
            {
                "user_id": "user-a",
                "memory": "same thread, same user",
                "metadata": {
                    "application": "threadwise",
                    "thread_id": "thread-a",
                    "channel_id": "channel-a",
                    "memory_kind": "summary",
                },
            },
            {
                "user_id": "user-a",
                "memory": "different thread",
                "metadata": {
                    "application": "threadwise",
                    "thread_id": "thread-b",
                    "channel_id": "channel-a",
                    "memory_kind": "summary",
                },
            },
            {
                "user_id": "user-b",
                "memory": "another user's context",
                "metadata": {
                    "application": "threadwise",
                    "thread_id": "thread-a",
                    "channel_id": "channel-a",
                    "memory_kind": "summary",
                },
            },
            {
                "user_id": "user-a",
                "memory": "same user's preference",
                "metadata": {
                    "application": "threadwise",
                    "thread_id": PREFERENCE_THREAD,
                    "channel_id": PREFERENCE_CHANNEL,
                    "memory_kind": "preference",
                },
            },
            {
                "user_id": "user-a",
                "memory": "unrelated application",
                "metadata": {
                    "application": "other-app",
                    "thread_id": "thread-a",
                    "channel_id": "channel-a",
                    "memory_kind": "summary",
                },
            },
        ]


def test_search_keeps_only_current_users_thread_and_preferences() -> None:
    memory = ScopedMemory.__new__(ScopedMemory)
    memory.api_key = "not-used"
    memory._client = FakeMem0Client()

    found = memory.search(
        user_id="user-a",
        thread_id="thread-a",
        channel_id="channel-a",
        query="pricing",
    )

    assert found == ["same thread, same user", "same user's preference"]
    assert memory._client.filters == {"user_id": "user-a"}
