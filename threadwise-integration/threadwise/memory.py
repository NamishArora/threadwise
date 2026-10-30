from __future__ import annotations

from typing import Any


PREFERENCE_THREAD = "__user_preference__"
PREFERENCE_CHANNEL = "__user_preference__"


class ScopedMemory:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ValueError("MEM0_API_KEY is required for memory access.")
            from mem0 import MemoryClient

            self._client = MemoryClient(api_key=self.api_key)
        return self._client

    @staticmethod
    def metadata(
        *,
        user_id: str,
        thread_id: str,
        channel_id: str,
        entity_name: str | None,
        thread_type: str,
        memory_kind: str,
    ) -> dict[str, str]:
        return {
            "user_id": user_id,
            "thread_id": thread_id,
            "channel_id": channel_id,
            "entity_name": entity_name or "",
            "thread_type": thread_type,
            "memory_kind": memory_kind,
            "application": "threadwise",
        }

    def search(
        self,
        *,
        user_id: str,
        thread_id: str,
        channel_id: str,
        query: str,
        limit: int = 6,
    ) -> list[str]:
        response = self._get_client().search(query, filters={"user_id": user_id})
        results = response.get("results", []) if isinstance(response, dict) else response
        scoped: list[str] = []
        for result in results or []:
            if not isinstance(result, dict) or result.get("user_id") != user_id:
                continue
            metadata = result.get("metadata") or {}
            if metadata.get("application") != "threadwise":
                continue
            is_current_thread = (
                metadata.get("thread_id") == thread_id
                and metadata.get("channel_id") == channel_id
            )
            is_user_preference = (
                metadata.get("memory_kind") == "preference"
                and metadata.get("thread_id") == PREFERENCE_THREAD
                and metadata.get("channel_id") == PREFERENCE_CHANNEL
            )
            if is_current_thread or is_user_preference:
                text = result.get("memory")
                if isinstance(text, str) and text:
                    scoped.append(text[:1000])
            if len(scoped) >= limit:
                break
        return scoped

    def add(
        self,
        *,
        user_id: str,
        content: str,
        metadata: dict[str, str],
    ) -> None:
        self._get_client().add(
            [{"role": "user", "content": content[:6000]}],
            user_id=user_id,
            metadata=metadata,
        )
