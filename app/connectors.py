from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from .models import Provider, SlackConversation, SourceRecord, may_use_slack_conversation


class ConnectorError(RuntimeError):
    pass


class SlackReader:
    def __init__(self, access_token: str, requester_id: str):
        self._requester_id = requester_id
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api/",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _api(self, method: str, **data: Any) -> dict[str, Any]:
        response = await self._client.post(method, data=data)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise ConnectorError(f"Slack 조회 실패: {payload.get('error', 'unknown_error')}")
        return payload

    async def _conversation(self, conversation_id: str) -> SlackConversation | None:
        try:
            info = await self._api("conversations.info", channel=conversation_id)
            channel = info["channel"]
            if not (channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim")):
                return SlackConversation(conversation_id, False, False, False, frozenset())
            members = await self._api("conversations.members", channel=conversation_id, limit=1000)
            return SlackConversation(
                conversation_id=conversation_id,
                is_private=bool(channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim")),
                is_im=bool(channel.get("is_im")),
                is_mpim=bool(channel.get("is_mpim")),
                member_ids=frozenset(members.get("members", [])),
            )
        except ConnectorError:
            # A source that cannot be re-checked is excluded rather than surfaced.
            return None

    async def search(
        self,
        query: str,
        limit: int = 20,
        *,
        only_conversation_id: str | None = None,
        exclude_message_ts: str | None = None,
    ) -> list[SourceRecord]:
        result = await self._api("search.messages", query=query, count=min(limit, 20), sort="timestamp")
        records: list[SourceRecord] = []
        checked: dict[str, SlackConversation | None] = {}
        for match in result.get("messages", {}).get("matches", []):
            conversation_id = match.get("channel", {}).get("id")
            if not conversation_id:
                continue
            if only_conversation_id and conversation_id != only_conversation_id:
                continue
            if exclude_message_ts and match.get("ts") == exclude_message_ts:
                # The question just sent to the app is input, not work-history
                # evidence. Never cite it back as an answer source.
                continue
            if conversation_id not in checked:
                checked[conversation_id] = await self._conversation(conversation_id)
            conversation = checked[conversation_id]
            if conversation is None or not may_use_slack_conversation(
                requester_id=self._requester_id, conversation=conversation
            ):
                continue
            timestamp = match.get("ts")
            permalink = None
            if timestamp:
                try:
                    permalink = (await self._api("chat.getPermalink", channel=conversation_id, message_ts=timestamp)).get(
                        "permalink"
                    )
                except ConnectorError:
                    pass
            records.append(
                SourceRecord(
                    provider=Provider.SLACK,
                    source_id=f"{conversation_id}:{timestamp or ''}",
                    title=match.get("channel", {}).get("name") or "Slack 대화",
                    occurred_at=match.get("ts"),
                    excerpt=match.get("text", "")[:4000],
                    source_url=permalink,
                    metadata={"conversation_id": conversation_id, "thread_ts": match.get("thread_ts")},
                )
            )
        return records


class JiraReader:
    def __init__(self, access_token: str, cloud_id: str):
        self._client = httpx.AsyncClient(
            base_url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=20,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, project_key: str = "GSM", limit: int = 20) -> list[SourceRecord]:
        escaped = query.replace('"', "\\\"")[:200]
        jql = f'project = {project_key} AND text ~ "{escaped}" ORDER BY updated DESC'
        response = await self._client.get(
            "search/jql",
            params={
                "jql": jql,
                "maxResults": min(limit, 50),
                "fields": "summary,description,status,updated,comment",
                "expand": "changelog",
            },
        )
        response.raise_for_status()
        payload = response.json()
        records: list[SourceRecord] = []
        for issue in payload.get("issues", []):
            fields = issue.get("fields", {})
            key = issue.get("key", issue["id"])
            description = _adf_to_text(fields.get("description"))
            comments = fields.get("comment", {}).get("comments", [])
            comment_text = "\n".join(_adf_to_text(item.get("body")) for item in comments)
            records.append(
                SourceRecord(
                    provider=Provider.JIRA,
                    source_id=str(issue["id"]),
                    title=f"{key}: {fields.get('summary', '')}",
                    occurred_at=fields.get("updated"),
                    excerpt=(description + "\n" + comment_text)[:8000],
                    source_url=f"https://scglab.atlassian.net/browse/{quote(key)}",
                    metadata={"issue_key": key, "status": (fields.get("status") or {}).get("name")},
                )
            )
        return records


class GmailReader:
    def __init__(self, access_token: str):
        self._client = httpx.AsyncClient(
            base_url="https://gmail.googleapis.com/gmail/v1/users/me/",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 20) -> list[SourceRecord]:
        response = await self._client.get("messages", params={"q": query[:500], "maxResults": min(limit, 50)})
        response.raise_for_status()
        records: list[SourceRecord] = []
        visited_threads: set[str] = set()
        for message in response.json().get("messages", []):
            message_response = await self._client.get(f"messages/{message['id']}", params={"format": "full"})
            message_response.raise_for_status()
            full = message_response.json()
            thread_id = full.get("threadId", full["id"])
            if thread_id in visited_threads:
                continue
            visited_threads.add(thread_id)
            headers = {item["name"].lower(): item["value"] for item in full.get("payload", {}).get("headers", [])}
            records.append(
                SourceRecord(
                    provider=Provider.GMAIL,
                    source_id=thread_id,
                    title=headers.get("subject", "제목 없는 메일"),
                    occurred_at=full.get("internalDate"),
                    excerpt=_gmail_text(full.get("payload", {}))[:8000],
                    # Gmail does not publish a stable REST permalink. Do not invent one.
                    source_url=None,
                    metadata={"thread_id": thread_id, "message_id": full["id"]},
                )
            )
        return records


def _adf_to_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text", "")
        return text + " ".join(_adf_to_text(item) for item in value.get("content", []))
    if isinstance(value, list):
        return " ".join(_adf_to_text(item) for item in value)
    return ""


def _gmail_text(part: dict[str, Any]) -> str:
    mime_type = part.get("mimeType", "")
    body = part.get("body", {}).get("data")
    pieces: list[str] = []
    if body and mime_type == "text/plain":
        try:
            pieces.append(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", "replace"))
        except ValueError:
            pass
    for child in part.get("parts", []):
        pieces.append(_gmail_text(child))
    return "\n".join(piece for piece in pieces if piece)
