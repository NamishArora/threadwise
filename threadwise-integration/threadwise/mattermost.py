from __future__ import annotations

from typing import Any

import httpx


class MattermostError(RuntimeError):
    pass


class AccessDenied(MattermostError):
    pass


class Mattermost:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, f"/api/v4{path}", **kwargs)
        if response.status_code in (401, 403, 404):
            raise AccessDenied(f"Mattermost denied access to {path} (HTTP {response.status_code}).")
        if response.is_error:
            raise MattermostError(f"Mattermost API request failed (HTTP {response.status_code}).")
        if not response.content:
            return None
        return response.json()

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/users/me")

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/users/{user_id}")

    def user_in_channel(self, channel_id: str, user_id: str) -> bool:
        self._request("GET", f"/channels/{channel_id}/members/{user_id}")
        return True

    def get_thread(self, post_id: str, per_page: int = 100) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/posts/{post_id}/thread",
            params={"perPage": per_page, "direction": "up"},
        )
        posts_by_id = result.get("posts", {})
        posts = list(posts_by_id.values())
        posts.sort(key=lambda post: (post.get("create_at", 0), post.get("id", "")))
        return posts[-per_page:]

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._request("GET", f"/channels/{channel_id}")

    def list_bot_channels(self, bot_user_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/users/{bot_user_id}/channels",
            params={"include_deleted": False},
        )
        return result if isinstance(result, list) else []

    def post_reply(self, channel_id: str, root_id: str, message: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/posts",
            json={"channel_id": channel_id, "root_id": root_id, "message": message},
        )

    def post_message(self, channel_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", "/posts", json={"channel_id": channel_id, "message": message})

    def create_direct_channel(self, bot_user_id: str, target_user_id: str) -> dict[str, Any]:
        return self._request("POST", "/channels/direct", json=[bot_user_id, target_user_id])

    def close(self) -> None:
        self.http.close()
