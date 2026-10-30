from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Commitment(BaseModel):
    promise: str = Field(description="A concrete promise made in this conversation.")
    promised_by: str | None = Field(default=None, description="Participant name, only when explicit.")
    due_date: date | None = Field(default=None, description="Explicit date only; otherwise null.")


class FollowUp(BaseModel):
    action: str
    owner: str | None = Field(default=None, description="Participant name, only when explicit.")
    due_date: date | None = Field(default=None, description="Explicit date only; otherwise null.")


class ThreadSummary(BaseModel):
    tldr: str = Field(description="Two or three concise sentences.")
    commitments: list[Commitment] = Field(default_factory=list)
    open_followups: list[FollowUp] = Field(default_factory=list)
    entity_name: str | None = None
    thread_type: Literal["client", "project", "opportunity", "internal"] = "internal"


class EmailSummary(BaseModel):
    tldr: str
    key_points: list[str] = Field(default_factory=list)
    action_items: list[FollowUp] = Field(default_factory=list)
    suggested_follow_up_date: date | None = None


class ThreadAnswer(BaseModel):
    answer: str
