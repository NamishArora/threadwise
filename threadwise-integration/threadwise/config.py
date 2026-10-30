from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mattermost_url: str
    mattermost_token: str
    slash_command_token: str
    mem0_api_key: str
    groq_api_key: str
    database_path: str
    fast_model: str
    summary_model: str
    commitment_days: int
    inactivity_days: int
    digest_time: str
    timezone: str
    max_thread_posts: int
    enable_websocket: bool
    enable_digests: bool
    summarize_slash_token: str = ""
    email_slash_token: str = ""
    threadwise_slash_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        shared_slash_token = os.getenv("MATTERMOST_SLASH_TOKEN", "")
        return cls(
            mattermost_url=os.getenv("MATTERMOST_URL", "http://host.docker.internal:8065").rstrip("/"),
            mattermost_token=os.getenv("MATTERMOST_BOT_TOKEN", ""),
            slash_command_token=shared_slash_token,
            mem0_api_key=os.getenv("MEM0_API_KEY", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            database_path=os.getenv("DATABASE_PATH", "data/threadwise.sqlite3"),
            fast_model=os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant"),
            summary_model=os.getenv("GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile"),
            commitment_days=int(os.getenv("COMMITMENT_DAYS", "3")),
            inactivity_days=int(os.getenv("INACTIVITY_DAYS", "14")),
            digest_time=os.getenv("DIGEST_TIME", "09:00"),
            timezone=os.getenv("TIMEZONE", "UTC"),
            max_thread_posts=int(os.getenv("MAX_THREAD_POSTS", "100")),
            enable_websocket=os.getenv("ENABLE_WEBSOCKET", "true").lower() == "true",
            enable_digests=os.getenv("ENABLE_DIGESTS", "true").lower() == "true",
            summarize_slash_token=os.getenv("MATTERMOST_SUMMARIZE_SLASH_TOKEN", shared_slash_token),
            email_slash_token=os.getenv("MATTERMOST_EMAIL_SLASH_TOKEN", shared_slash_token),
            threadwise_slash_token=os.getenv("MATTERMOST_THREADWISE_SLASH_TOKEN", shared_slash_token),
        )

    def slash_token_for(self, command: str) -> str:
        tokens = {
            "/summarize": self.summarize_slash_token,
            "/threadwise-email": self.email_slash_token,
            "/threadwise": self.threadwise_slash_token,
        }
        return tokens[command] or self.slash_command_token

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("MATTERMOST_BOT_TOKEN", self.mattermost_token),
                ("MEM0_API_KEY", self.mem0_api_key),
                ("GROQ_API_KEY", self.groq_api_key),
            )
            if not value
        ]
        missing.extend(
            name
            for name, command in (
                ("MATTERMOST_SUMMARIZE_SLASH_TOKEN", "/summarize"),
                ("MATTERMOST_EMAIL_SLASH_TOKEN", "/threadwise-email"),
                ("MATTERMOST_THREADWISE_SLASH_TOKEN", "/threadwise"),
            )
            if not self.slash_token_for(command)
        )
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        if self.commitment_days < 1 or self.inactivity_days < 1:
            raise ValueError("Commitment and inactivity thresholds must be positive.")
        if not 1 <= self.max_thread_posts <= 500:
            raise ValueError("MAX_THREAD_POSTS must be between 1 and 500.")
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            datetime.strptime(self.digest_time, "%H:%M")
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as error:
            raise ValueError("DIGEST_TIME must be HH:MM and TIMEZONE must be a valid IANA zone.") from error
