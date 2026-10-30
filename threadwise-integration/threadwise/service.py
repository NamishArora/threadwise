from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .ai import GroqAgents, GroqQuotaExceeded, encode_schema
from .config import Settings
from .database import Database
from .mattermost import AccessDenied, Mattermost
from .memory import PREFERENCE_CHANNEL, PREFERENCE_THREAD, ScopedMemory
from .schemas import EmailSummary, ThreadSummary

logger = logging.getLogger(__name__)
MENTION = re.compile(r"(?:^|\s)@(?:AI|threadwise)\b[\s,:-]*(.*)$", re.IGNORECASE | re.DOTALL)
MAX_CONTEXT_CHARS = 12_000


@dataclass(frozen=True)
class ThreadSnapshot:
    channel_id: str
    channel_name: str
    root_id: str
    latest_post_id: str
    latest_create_at: int
    posts: list[dict[str, Any]]
    participants: dict[str, str]

    def prompt_text(self) -> str:
        lines: list[str] = []
        for post in self.posts:
            name = self.participants.get(post.get("user_id", ""), "Unknown participant")
            message = str(post.get("message", "")).replace("\x00", "")[:1000]
            lines.append(f"[{name}] {message}")
        text = "\n".join(lines)
        return text[-MAX_CONTEXT_CHARS:]


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def render_summary(summary: dict[str, Any]) -> str:
    lines = ["**TL;DR**", summary.get("tldr", "No summary available.")]
    commitments = summary.get("commitments", [])
    lines.append("\n**Commitments made**")
    if commitments:
        for item in commitments[:8]:
            owner = item.get("promised_by") or "Owner not explicit"
            date = f" — due {item['due_date']}" if item.get("due_date") else ""
            lines.append(f"- {owner}: {item['promise']}{date}")
    else:
        lines.append("- No explicit commitments identified.")
    followups = summary.get("open_followups", [])
    lines.append("\n**Open follow-ups**")
    if followups:
        for item in followups[:8]:
            owner = item.get("owner") or "Owner unassigned"
            date = f" — due {item['due_date']}" if item.get("due_date") else ""
            lines.append(f"- {item['action']} ({owner}){date}")
    else:
        lines.append("- No open follow-ups identified.")
    return "\n".join(lines)


def render_email(summary: dict[str, Any]) -> str:
    lines = ["**TL;DR**", summary.get("tldr", "")]
    points = summary.get("key_points", [])
    lines.append("\n**Key points**")
    lines.extend(f"- {point}" for point in points[:8] if point)
    actions = summary.get("action_items", [])
    lines.append("\n**Action items**")
    lines.extend(
        f"- {item.get('action', '')} ({item.get('owner') or 'Owner unassigned'})"
        for item in actions[:8]
    )
    if not actions:
        lines.append("- None detected.")
    if summary.get("suggested_follow_up_date"):
        lines.append(f"\nSuggested follow-up: {summary['suggested_follow_up_date']}")
    return "\n".join(lines)


class ThreadwiseService:
    def __init__(
        self,
        settings: Settings,
        mattermost: Mattermost,
        memory: ScopedMemory,
        agents: GroqAgents,
        database: Database,
    ) -> None:
        self.settings = settings
        self.mm = mattermost
        self.memory = memory
        self.agents = agents
        self.db = database
        self.bot_user_id = ""

    def initialize(self) -> None:
        me = self.mm.get_me()
        self.bot_user_id = me["id"]

    def _snapshot(self, post_id: str, channel_id: str, user_id: str) -> ThreadSnapshot:
        if not re.fullmatch(r"[a-z0-9]{26}", post_id) or not re.fullmatch(r"[a-z0-9]{26}", channel_id):
            raise ValueError("Provide a valid Mattermost post ID and channel.")
        self.mm.user_in_channel(channel_id, user_id)
        posts = self.mm.get_thread(post_id, per_page=self.settings.max_thread_posts)
        if not posts:
            raise AccessDenied("That thread is unavailable to the bot.")
        for post in posts:
            if post.get("channel_id") != channel_id:
                raise AccessDenied("The requested post is not in this channel.")
        post = next((item for item in posts if item.get("id") == post_id), None)
        if post is None:
            raise AccessDenied("The requested post is not part of the retrieved thread.")
        root_id = post.get("root_id") or post["id"]
        posts = [item for item in posts if (item.get("root_id") or item.get("id")) == root_id]
        if not posts:
            raise AccessDenied("The requested thread is unavailable.")
        posts.sort(key=lambda item: (item.get("create_at", 0), item.get("id", "")))
        channel = self.mm.get_channel(channel_id)
        participants: dict[str, str] = {}
        for participant_id in {item.get("user_id") for item in posts if item.get("user_id")}:
            try:
                person = self.mm.get_user(participant_id)
                participants[participant_id] = (
                    person.get("username") or person.get("first_name") or "Participant"
                )
            except AccessDenied:
                participants[participant_id] = "Participant"
        newest = posts[-1]
        for item in posts:
            self.db.record_activity(item, channel.get("name", ""))
        return ThreadSnapshot(
            channel_id=channel_id,
            channel_name=channel.get("name", ""),
            root_id=root_id,
            latest_post_id=newest["id"],
            latest_create_at=int(newest.get("create_at", 0)),
            posts=posts,
            participants=participants,
        )

    def _memories(self, user_id: str, snapshot: ThreadSnapshot, query: str) -> list[str]:
        return self.memory.search(
            user_id=user_id,
            thread_id=snapshot.root_id,
            channel_id=snapshot.channel_id,
            query=query,
        )

    def answer(self, post_id: str, channel_id: str, user_id: str, question: str) -> str:
        if not question.strip():
            return "Ask a question after `@AI`."
        snapshot = self._snapshot(post_id, channel_id, user_id)
        memories = self._memories(user_id, snapshot, question)
        try:
            result = self.agents.answer(
                question,
                memories,
                read_context=snapshot.prompt_text,
            )
            return result.answer[:3000]
        except GroqQuotaExceeded:
            cached = self.db.get_summary(user_id, snapshot.root_id)
            return self.agents.quota_message(cached)

    def summarize(self, post_id: str, channel_id: str, user_id: str) -> tuple[str, ThreadSnapshot]:
        snapshot = self._snapshot(post_id, channel_id, user_id)
        cached = self.db.get_summary(user_id, snapshot.root_id)
        if cached and cached["latest_post_id"] == snapshot.latest_post_id:
            if not cached["memory_synced"]:
                metadata = self.memory.metadata(
                    user_id=user_id,
                    thread_id=snapshot.root_id,
                    channel_id=channel_id,
                    entity_name=cached["summary"].get("entity_name"),
                    thread_type=cached["summary"].get("thread_type", "internal"),
                    memory_kind="summary",
                )
                self.memory.add(
                    user_id=user_id,
                    content=render_summary(cached["summary"]),
                    metadata=metadata,
                )
                self.db.mark_summary_memory_synced(user_id, snapshot.root_id)
            return render_summary(cached["summary"]), snapshot

        memories = self._memories(user_id, snapshot, "thread commitments actions and relationship context")
        try:
            result = self.agents.summarize(memories, snapshot.prompt_text)
        except GroqQuotaExceeded:
            if cached:
                return self.agents.quota_message(cached), snapshot
            raise
        if not isinstance(result, ThreadSummary):
            raise TypeError("The agent returned an email schema for a thread summary.")
        summary = encode_schema(result)
        owner_ids = {
            _normalized(name): user_id
            for user_id, name in snapshot.participants.items()
        }
        # Preserve the last activity timestamp so a later message is distinguishable from the promise.
        self.db.save_summary(
            user_id=user_id,
            channel_id=channel_id,
            thread_id=snapshot.root_id,
            latest_post_id=snapshot.latest_post_id,
            summary=summary,
            channel_name=snapshot.channel_name,
            owner_ids=owner_ids,
            detected_at=snapshot.latest_create_at,
            memory_synced=False,
        )
        metadata = self.memory.metadata(
            user_id=user_id,
            thread_id=snapshot.root_id,
            channel_id=channel_id,
            entity_name=result.entity_name,
            thread_type=result.thread_type,
            memory_kind="summary",
        )
        content = render_summary(summary)
        self.memory.add(user_id=user_id, content=content, metadata=metadata)
        self.db.mark_summary_memory_synced(user_id, snapshot.root_id)
        return content, snapshot

    def summarize_email(self, user_id: str, channel_id: str, email_text: str) -> tuple[str, str, dict[str, Any]]:
        if not email_text.strip():
            raise ValueError("Paste email text after `/threadwise-email`.")
        if len(email_text) > 20_000:
            raise ValueError("Email text is too long; limit it to 20,000 characters.")
        result = self.agents.summarize(
            [],
            lambda: email_text[:12_000],
            email=True,
        )
        if not isinstance(result, EmailSummary):
            raise TypeError("The agent returned a thread schema for an email summary.")
        summary = encode_schema(result)
        draft_id = self.db.make_email_draft(user_id, channel_id, summary)
        return render_email(summary), draft_id, summary

    def attach_email(self, draft_id: str, post_id: str, channel_id: str, user_id: str) -> str:
        draft = self.db.get_email_draft(draft_id, user_id)
        if draft is None:
            raise ValueError("Email summary not found for this user; summarize the email again.")
        snapshot = self._snapshot(post_id, channel_id, user_id)
        summary = draft["summary"]
        text = "**Email context attached**\n\n" + render_email(summary)
        metadata = self.memory.metadata(
            user_id=user_id,
            thread_id=snapshot.root_id,
            channel_id=channel_id,
            entity_name=None,
            thread_type="internal",
            memory_kind="email_summary",
        )
        self.memory.add(user_id=user_id, content=text, metadata=metadata)
        self.mm.post_reply(channel_id, snapshot.root_id, text)
        return "Email summary attached to the thread and saved to your private Threadwise memory."

    def remember_preference(self, user_id: str, preference: str) -> str:
        text = preference.strip()
        if not text or len(text) > 1000:
            raise ValueError("Provide a preference between 1 and 1,000 characters.")
        metadata = self.memory.metadata(
            user_id=user_id,
            thread_id=PREFERENCE_THREAD,
            channel_id=PREFERENCE_CHANNEL,
            entity_name=None,
            thread_type="internal",
            memory_kind="preference",
        )
        self.memory.add(user_id=user_id, content=text, metadata=metadata)
        return "Saved as your private Threadwise preference."

    def record_post(self, post: dict[str, Any], channel_name: str = "") -> None:
        self.db.record_activity(post, channel_name)

    def process_websocket_post(self, post: dict[str, Any]) -> None:
        if not post or post.get("user_id") == self.bot_user_id:
            return
        channel_id = post.get("channel_id", "")
        root_id = post.get("root_id") or post.get("id", "")
        text = str(post.get("message", ""))
        match = MENTION.search(text)
        if not channel_id or not root_id:
            return
        self.db.record_activity(post)
        if not match:
            return
        user_id = post.get("user_id")
        if not user_id:
            return
        try:
            self.mm.user_in_channel(channel_id, user_id)
            answer = self.answer(root_id, channel_id, user_id, match.group(1).strip())
            self.mm.post_reply(channel_id, root_id, answer)
        except AccessDenied:
            logger.info("Skipped AI request because the requester cannot access this channel.")
        except Exception:
            logger.exception("Could not process an AI mention.")

    def run_digest_for_user(self, user_id: str, local_date: str | None = None) -> bool:
        now = datetime.now(UTC)
        local_date = local_date or now.date().isoformat()
        if self.db.digest_already_sent(user_id, local_date):
            return False
        candidates = self.db.digest_items(
            user_id,
            now_ms=int(now.timestamp() * 1000),
            commitment_days=self.settings.commitment_days,
            inactivity_days=self.settings.inactivity_days,
        )
        items = []
        for candidate in candidates:
            try:
                self.mm.user_in_channel(candidate["channel_id"], user_id)
            except AccessDenied:
                continue
            items.append(candidate["text"])
        if not items:
            self.db.mark_digest_sent(user_id, local_date)
            return False
        channel = self.mm.create_direct_channel(self.bot_user_id, user_id)
        text = "**Threadwise: threads needing attention**\n" + "\n".join(
            f"- {item}" for item in items
        )
        self.mm.post_message(channel["id"], text)
        self.db.mark_digest_sent(user_id, local_date)
        return True

    def run_digests(self, local_date: str | None = None) -> bool:
        succeeded = True
        for user_id in self.db.digest_users():
            try:
                self.run_digest_for_user(user_id, local_date)
            except Exception:
                logger.exception("Could not deliver a Threadwise digest.")
                succeeded = False
        return succeeded
