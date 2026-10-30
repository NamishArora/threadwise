from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager, closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4


class Database:
    def __init__(self, path: str) -> None:
        self._uri = path == ":memory:"
        self.path = f"file:threadwise-{id(self)}?mode=memory&cache=shared" if self._uri else path
        self._lock = threading.RLock()
        self._memory_keeper = self.connect() if self._uri else None
        if not self._uri:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self):
        with closing(self.connect()) as db:
            with db:
                yield db

    def initialize(self) -> None:
        with self._lock, self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    latest_post_id TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    memory_synced INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS thread_activity (
                    channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    latest_post_id TEXT NOT NULL,
                    latest_activity_at INTEGER NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (channel_id, thread_id)
                );
                CREATE TABLE IF NOT EXISTS commitments (
                    commitment_id TEXT PRIMARY KEY,
                    owner_user_id TEXT,
                    created_by_user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    owner_name TEXT,
                    due_date TEXT,
                    detected_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS commitments_owner_idx
                    ON commitments(owner_user_id, detected_at);
                CREATE TABLE IF NOT EXISTS digest_subscriptions (
                    user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS digest_runs (
                    user_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, local_date)
                );
                CREATE TABLE IF NOT EXISTS email_drafts (
                    draft_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            summary_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(summary_cache)")
            }
            if "memory_synced" not in summary_columns:
                db.execute(
                    "ALTER TABLE summary_cache ADD COLUMN memory_synced INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    def save_summary(
        self,
        *,
        user_id: str,
        channel_id: str,
        thread_id: str,
        latest_post_id: str,
        summary: dict[str, Any],
        channel_name: str,
        owner_ids: dict[str, str],
        detected_at: int,
        memory_synced: bool = False,
    ) -> None:
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO summary_cache
                    (user_id, channel_id, thread_id, latest_post_id, summary_json, updated_at, memory_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, thread_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    latest_post_id=excluded.latest_post_id,
                    summary_json=excluded.summary_json,
                    updated_at=excluded.updated_at,
                    memory_synced=excluded.memory_synced
                """,
                (
                    user_id,
                    channel_id,
                    thread_id,
                    latest_post_id,
                    json.dumps(summary),
                    detected_at,
                    int(memory_synced),
                ),
            )
            db.execute(
                "DELETE FROM commitments WHERE created_by_user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )
            for followup in summary.get("open_followups", []):
                owner_name = followup.get("owner")
                owner_id = owner_ids.get(owner_name.casefold()) if owner_name else None
                action = followup.get("action", "")[:500]
                commitment_id = sha256(
                    f"{thread_id}|{owner_id or ''}|{action.casefold()}".encode("utf-8")
                ).hexdigest()
                db.execute(
                    """
                    INSERT INTO commitments
                        (commitment_id, owner_user_id, created_by_user_id, channel_id, thread_id,
                         channel_name, action, owner_name, due_date, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(commitment_id) DO UPDATE SET
                        created_by_user_id=excluded.created_by_user_id,
                        channel_id=excluded.channel_id,
                        channel_name=excluded.channel_name,
                        action=excluded.action,
                        owner_name=excluded.owner_name,
                        due_date=excluded.due_date,
                        detected_at=excluded.detected_at
                    """,
                    (
                        commitment_id,
                        owner_id,
                        user_id,
                        channel_id,
                        thread_id,
                        channel_name,
                        action,
                        owner_name,
                        followup.get("due_date"),
                        detected_at,
                    ),
                )

    def get_summary(self, user_id: str, thread_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM summary_cache WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "channel_id": row["channel_id"],
            "latest_post_id": row["latest_post_id"],
            "summary": json.loads(row["summary_json"]),
            "memory_synced": bool(row["memory_synced"]),
        }

    def mark_summary_memory_synced(self, user_id: str, thread_id: str) -> None:
        with self._lock, self.connection() as db:
            db.execute(
                "UPDATE summary_cache SET memory_synced = 1 WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )

    def record_activity(self, post: dict[str, Any], channel_name: str = "") -> None:
        channel_id = post.get("channel_id")
        thread_id = post.get("root_id") or post.get("id")
        post_id = post.get("id")
        created_at = post.get("create_at")
        if not all((channel_id, thread_id, post_id)) or not isinstance(created_at, int):
            return
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO thread_activity
                    (channel_id, thread_id, latest_post_id, latest_activity_at, channel_name)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, thread_id) DO UPDATE SET
                    latest_post_id=CASE
                        WHEN excluded.latest_activity_at >= thread_activity.latest_activity_at
                        THEN excluded.latest_post_id ELSE thread_activity.latest_post_id END,
                    latest_activity_at=MAX(thread_activity.latest_activity_at, excluded.latest_activity_at),
                    channel_name=CASE WHEN excluded.channel_name != ''
                        THEN excluded.channel_name ELSE thread_activity.channel_name END
                """,
                (channel_id, thread_id, post_id, created_at, channel_name),
            )

    def set_digest_enabled(self, user_id: str, enabled: bool) -> None:
        with self._lock, self.connection() as db:
            db.execute(
                """
                INSERT INTO digest_subscriptions(user_id, enabled, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at
                """,
                (user_id, int(enabled), self.now_ms()),
            )

    def digest_users(self) -> list[str]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id FROM digest_subscriptions WHERE enabled = 1"
            ).fetchall()
        return [row["user_id"] for row in rows]

    def make_email_draft(self, user_id: str, channel_id: str, summary: dict[str, Any]) -> str:
        draft_id = uuid4().hex[:12]
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO email_drafts VALUES (?, ?, ?, ?, ?)",
                (draft_id, user_id, channel_id, json.dumps(summary), self.now_ms()),
            )
        return draft_id

    def get_email_draft(self, draft_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT channel_id, summary_json FROM email_drafts WHERE draft_id = ? AND user_id = ?",
                (draft_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {"channel_id": row["channel_id"], "summary": json.loads(row["summary_json"])}

    def digest_items(
        self,
        user_id: str,
        *,
        now_ms: int,
        commitment_days: int,
        inactivity_days: int,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        commitment_cutoff = now_ms - commitment_days * 86_400_000
        inactive_cutoff = now_ms - inactivity_days * 86_400_000
        with self.connection() as db:
            commitments = db.execute(
                """
                SELECT c.channel_id, c.action, c.channel_name, c.owner_name
                FROM commitments c
                JOIN thread_activity a
                  ON a.channel_id = c.channel_id AND a.thread_id = c.thread_id
                WHERE c.owner_user_id = ?
                  AND c.detected_at <= ?
                  AND a.latest_activity_at <= c.detected_at
                ORDER BY c.detected_at ASC LIMIT ?
                """,
                (user_id, commitment_cutoff, limit),
            ).fetchall()
            inactive = db.execute(
                """
                SELECT DISTINCT s.thread_id, s.channel_id, s.summary_json,
                       a.channel_name, a.latest_activity_at
                FROM summary_cache s
                JOIN thread_activity a
                  ON a.channel_id = s.channel_id AND a.thread_id = s.thread_id
                WHERE s.user_id = ? AND a.latest_activity_at <= ?
                ORDER BY a.latest_activity_at ASC LIMIT ?
                """,
                (user_id, inactive_cutoff, limit),
            ).fetchall()
        items = [
            {
                "channel_id": row["channel_id"],
                "text": f"{row['action']} ({row['owner_name'] or 'owner unassigned'}) — "
                f"{row['channel_name'] or 'channel'}; no newer thread activity",
            }
            for row in commitments
        ]
        for row in inactive:
            summary = json.loads(row["summary_json"])
            entity = summary.get("entity_name") or "Relationship"
            text = (
                f"{entity} — {row['channel_name'] or 'channel'}; "
                f"no thread activity for {inactivity_days}+ days"
            )
            if all(item["text"] != text for item in items):
                items.append({"channel_id": row["channel_id"], "text": text})
        return items[:limit]

    def digest_already_sent(self, user_id: str, local_date: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT 1 FROM digest_runs WHERE user_id = ? AND local_date = ?",
                (user_id, local_date),
            ).fetchone()
        return row is not None

    def mark_digest_sent(self, user_id: str, local_date: str) -> None:
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO digest_runs VALUES (?, ?, ?)",
                (user_id, local_date, self.now_ms()),
            )
