from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Provider(str, Enum):
    SLACK = "slack"
    JIRA = "jira"
    GMAIL = "gmail"


@dataclass(frozen=True)
class SourceRecord:
    provider: Provider
    source_id: str
    title: str
    occurred_at: str | None
    excerpt: str
    source_url: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SlackConversation:
    conversation_id: str
    is_private: bool
    is_im: bool
    is_mpim: bool
    member_ids: frozenset[str]


def may_use_slack_conversation(*, requester_id: str, conversation: SlackConversation) -> bool:
    """Fail closed for non-public conversations; do not reveal their existence."""
    if not conversation.is_private:
        return True
    return requester_id in conversation.member_ids
