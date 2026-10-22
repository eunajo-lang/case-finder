from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .models import Provider, SlackConversation, SourceRecord, may_use_slack_conversation


_QUERY_FILLER_WORDS = {
    "관련",
    "이슈",
    "어떻게",
    "알려줘",
    "알려",
    "주세요",
    "부탁",
    "민원",
    "문의",
    "확인",
    "정상",
    "이용",
    "경우",
    "이런",
    "이러한",
    "무엇",
    "무슨",
    "왜",
    "아직",
    "그런",
    "대한",
}
_KOREAN_ENDINGS = (
    "했는데",
    "하는데",
    "되는데",
    "됐는데",
    "인데",
    "는데",
    "합니다",
    "했습니다",
    "해주세요",
    "되나요",
    "인가요",
    "입니다",
    "이었어요",
    "었어",
    "았어",
    "냈어",
    "었어요",
    "아요",
    "어요",
    "으로",
    "에서",
    "에게",
    "에는",
    "으로",
    "이고",
    "이며",
    "이",
    "가",
    "은",
    "는",
    "을",
    "를",
    "에",
)
_INCIDENT_TERMS = (
    "신청",
    "등록",
    "조회",
    "미납",
    "주소",
    "오류",
    "고지서",
    "카드",
    "해지",
    "동기화",
    "입력",
    "미입력",
    "불가",
)
_SUBJECT_TERMS = (
    "전기요금",
    "도시가스",
    "가스앱",
    "전기",
    "가스",
    "요금",
    "결제",
    "청구",
    "계약",
    "계정",
    "서비스",
)
_INCIDENT_EXPANSIONS = {
    "신청": ("신청", "등록", "조회"),
    "등록": ("등록", "신청", "조회"),
    "조회": ("조회", "등록", "신청"),
    "미납": ("미납", "고지서", "납부"),
    "고지서": ("고지서", "미납", "납부"),
    "주소": ("주소", "정보", "정정"),
    "오류": ("오류", "등록", "조회"),
    "카드": ("카드", "계약", "조회"),
    "해지": ("해지", "재신청", "등록"),
    "동기화": ("동기화", "등록", "조회"),
    "입력": ("입력", "미입력", "불가"),
    "미입력": ("미입력", "입력", "불가"),
    "불가": ("불가", "입력", "오류"),
}

# These terms describe the business feature or incident. They must take
# precedence over conversational words such as "이번" or "히스토리".
_DOMAIN_FEATURE_TERMS = (
    "자가검침",
    "검침일자",
    "확정일자",
    "미납",
    "확정",
    "노출",
    "청구서",
    "직수납",
    "고지서",
    "동기화",
    "입력",
    "미입력",
    "오류",
)

_QUERY_CONTEXT_TERMS = {
    "고객",
    "기간",
    "중",
    "입력",
    "미입력",
    "불가",
    "안된다",
    "안됨",
    "안되",
    "보냈",
    "보냈어",
    "변경",
    "해야",
    "된대",
    "쿼리",
    "뭐였지",
    "들어왔",
    "이번",
    "또",
    "전에",
    "작업",
    "히스토리",
    "보여줘",
}


@dataclass(frozen=True)
class SearchIntent:
    """The minimum incident context needed before retrieving company records.

    This is deliberately deterministic. A user should not be blocked from a
    history search merely because an external summarisation model is unavailable.
    """

    subject: str | None
    features: tuple[str, ...]
    request_type: str
    time_context: str | None
    clarification_prompt: str | None


def interpret_search_intent(question: str) -> SearchIntent:
    """Extract search intent and ask only when no incident can be identified.

    Conversational time words, for example ``이번``, are retained as context for
    a later answer but never become evidence-search terms. A company name alone
    is too broad: it would invite unrelated meetings or announcements into an
    incident answer.
    """
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
    tokens = _question_tokens(question)
    anchor = next((term for term in _SUBJECT_TERMS if term in normalized), None)
    subject = next(
        (token for token in tokens if anchor and token != anchor and anchor in token),
        None,
    )
    features = tuple(term for term in _DOMAIN_FEATURE_TERMS if term in normalized)
    if not features:
        features = tuple(term for term in _INCIDENT_TERMS if term in normalized)

    if any(marker in normalized for marker in ("쿼리", "sql", "조회식", "where절")):
        request_type = "lookup"
    elif any(marker in normalized for marker in ("왜", "원인", "이유")):
        request_type = "cause"
    elif any(marker in normalized for marker in ("어떻게 처리", "처리", "대응", "해결")):
        request_type = "resolution"
    else:
        request_type = "history"

    time_context = next(
        (marker for marker in ("오늘", "어제", "이번", "최근", "지난", "작년", "올해") if marker in normalized),
        None,
    )
    if features:
        clarification_prompt = None
    elif subject:
        clarification_prompt = (
            "검색할 사건을 조금만 구체적으로 알려주세요. "
            "예: ‘미납요금 확정 전 노출’, ‘자가검침 입력 불가’처럼 기능이나 현상을 적어 주세요."
        )
    else:
        clarification_prompt = (
            "정확한 사례를 찾으려면 대상 서비스(또는 회사)와 현상을 알려주세요. "
            "예: ‘서울도시가스 고객의 미납요금 확정 전 노출’처럼 적어 주세요."
        )
    return SearchIntent(
        subject=subject,
        features=features,
        request_type=request_type,
        time_context=time_context,
        clarification_prompt=clarification_prompt,
    )


def _question_tokens(question: str) -> list[str]:
    """Extract stable lexical terms from a Korean natural-language question."""
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
    tokens: list[str] = []
    for raw in re.findall(r"[0-9A-Za-z가-힣]{2,}", normalized):
        token = raw.lower()
        for ending in _KOREAN_ENDINGS:
            if token.endswith(ending) and len(token) - len(ending) >= 2:
                token = token[: -len(ending)]
                break
        if token and token not in _QUERY_FILLER_WORDS and token not in tokens:
            tokens.append(token)
    return tokens


def slack_focus_terms(question: str) -> list[str]:
    """Return distinctive business terms used to reject broad, unrelated hits.

    Subject words such as ``도시가스`` occur in too many unrelated discussions.
    A longer company/product term (``서울도시가스``) and a feature term
    (``자가검침``) carry much more retrieval signal, so retain them separately.
    """
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
    tokens = _question_tokens(question)
    anchor = next((term for term in _SUBJECT_TERMS if term in normalized), None)
    focus: list[str] = []
    for token in tokens:
        is_specific_subject = bool(anchor and token != anchor and anchor in token)
        is_feature = token not in _QUERY_CONTEXT_TERMS and token not in _SUBJECT_TERMS
        if (is_specific_subject or is_feature) and token not in focus:
            focus.append(token)
    specific_subjects = [term for term in focus if anchor and term != anchor and anchor in term]
    domain_features = [term for term in _DOMAIN_FEATURE_TERMS if term in normalized]
    if domain_features:
        # Prefer issue vocabulary over the position of a word in a sentence.
        # For example, "이번에 또 미납요금 확정 작업 전에 노출" must
        # become "미납/확정/노출", never "이번".
        focus = specific_subjects + [term for term in domain_features if term not in specific_subjects]
    if specific_subjects:
        # For a named company/product, its first feature term is the useful
        # discriminator. Keeping question phrasing such as "쿼리가 뭐였지" as
        # a focus term lets unrelated threads look falsely similar.
        features = [term for term in focus if term not in specific_subjects]
        return specific_subjects[:1] + features[:3]
    return focus


def slack_minimum_focus_matches(question: str) -> int:
    """Return how many distinctive terms a case must contain to be comparable.

    A named company plus a feature is an AND condition, not an OR condition.
    For example, a company-wide contract meeting is not a comparable incident
    for a question about that company's self-meter-reading date.
    """
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
    anchor = next((term for term in _SUBJECT_TERMS if term in normalized), None)
    focus = slack_focus_terms(question)
    has_named_subject = bool(anchor and any(term != anchor and anchor in term for term in focus))
    return 2 if has_named_subject and len(focus) >= 2 else 1


def slack_query_variants(question: str, maximum: int = 4) -> list[str]:
    """Create a small, deterministic set of Slack full-text queries.

    Slack's search API is lexical. Passing an entire natural-language complaint
    makes every word mandatory and often hides useful historical cases, so use
    a subject anchor and a few symptom variants instead. This is deliberately
    bounded to limit data retrieval and API calls.
    """
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
    tokens = _question_tokens(question)

    if not tokens:
        return []

    # Prefer an identifiable business object over an adjective such as "정상적".
    anchor = next((term for term in _SUBJECT_TERMS if term in normalized), max(tokens, key=len))
    focus = slack_focus_terms(question)
    specific_subjects = [term for term in focus if anchor in term and term != anchor]
    feature_terms = [term for term in focus if term not in specific_subjects]

    # An organization/product plus its feature is more discriminating than the
    # broad subject. Search it before expanding generic incident vocabulary.
    variants: list[str] = []
    if specific_subjects:
        if feature_terms:
            variants.append(f"{specific_subjects[0]} {feature_terms[0]}")
            if len(feature_terms) >= 2:
                variants.append(f"{specific_subjects[0]} {feature_terms[1]}")
                variants.append(f"{feature_terms[0]} {feature_terms[1]}")
            elif any(term in normalized for term in ("입력", "미입력", "불가", "안되")):
                variants.append(f"{feature_terms[0]} 입력")
            else:
                variants.append(feature_terms[0])
            if len(feature_terms) >= 3:
                variants.append(f"{feature_terms[0]} {feature_terms[2]}")
        variants.append(specific_subjects[0])
    else:
        variants.append(anchor)
    incident_terms: list[str] = []
    for marker, expansions in _INCIDENT_EXPANSIONS.items():
        if marker in normalized:
            incident_terms.extend(expansions)
    incident_terms.extend(term for term in _INCIDENT_TERMS if term in normalized)
    if not incident_terms:
        incident_terms.extend(token for token in tokens if token != anchor)
    for term in incident_terms:
        if term != anchor:
            variants.append(f"{anchor} {term}")

    unique: list[str] = []
    for query in variants:
        if query not in unique:
            unique.append(query)
        if len(unique) >= max(1, min(maximum, 4)):
            break
    return unique


def _slack_timestamp_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
        except (ConnectorError, httpx.HTTPError):
            # A source that cannot be re-checked is excluded rather than surfaced.
            return None

    async def search_cases(
        self,
        question: str,
        *,
        limit: int = 4,
        exclude_conversation_id: str | None = None,
    ) -> list[SourceRecord]:
        """Return Slack evidence grouped by incident thread, never by individual hit.

        All visibility checks happen before a thread is read.  A public channel
        is eligible even if the requester is not a member; a private
        channel/DM/MPDM is eligible only while the requester is a member.
        """
        queries = slack_query_variants(question)
        if not queries:
            return []
        focus_terms = slack_focus_terms(question)
        minimum_focus_matches = slack_minimum_focus_matches(question)
        normalized_question = re.sub(r"[^0-9A-Za-z가-힣]+", " ", question).strip()
        subject_anchor = next((term for term in _SUBJECT_TERMS if term in normalized_question), None)
        focus_weights = {
            term: 3 if subject_anchor and term != subject_anchor and subject_anchor in term else 1
            for term in focus_terms
        }
        query_rank = {query: len(queries) - index for index, query in enumerate(queries)}

        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for query in queries:
            result = await self._api(
                "search.messages",
                query=query,
                count=min(max(limit * 3, 12), 20),
                # Slack's default relevance ordering is intentionally used here;
                # timestamp ordering was surfacing recent but unrelated deploy logs.
                sort="score",
            )
            for match in result.get("messages", {}).get("matches", []):
                conversation_id = match.get("channel", {}).get("id")
                timestamp = match.get("ts")
                if not conversation_id or not timestamp or conversation_id == exclude_conversation_id:
                    continue
                root_ts = match.get("thread_ts") or timestamp
                key = (conversation_id, root_ts)
                candidate = candidates.setdefault(
                    key,
                    {
                        "conversation_id": conversation_id,
                        "root_ts": root_ts,
                        "channel_name": match.get("channel", {}).get("name") or "Slack 대화",
                        "matched_queries": set(),
                        "matched_timestamps": set(),
                        "search_may_be_incomplete": False,
                    },
                )
                candidate["matched_queries"].add(query)
                candidate["matched_timestamps"].add(timestamp)
                messages = result.get("messages", {})
                paging = messages.get("paging", {})
                candidate["search_may_be_incomplete"] = candidate["search_may_be_incomplete"] or bool(
                    result.get("response_metadata", {}).get("next_cursor")
                    or paging.get("page", 1) < paging.get("pages", 1)
                )

        def priority(candidate: dict[str, Any]) -> tuple[int, int, int, float]:
            return (
                sum(query_rank[query] for query in candidate["matched_queries"]),
                len(candidate["matched_queries"]),
                len(candidate["matched_timestamps"]),
                _slack_timestamp_number(candidate["root_ts"]),
            )

        records: list[SourceRecord] = []
        checked: dict[str, SlackConversation | None] = {}
        # First deduplicate raw search hits. Only then re-check a small number
        # of candidate conversations and read up to `limit` whole threads.
        # This avoids a conversations.info request for every lexical hit.
        candidate_review_limit = max(4, min(max(limit * 2, 4), 8))
        for candidate in sorted(candidates.values(), key=priority, reverse=True)[:candidate_review_limit]:
            conversation_id = candidate["conversation_id"]
            root_ts = candidate["root_ts"]
            if conversation_id not in checked:
                checked[conversation_id] = await self._conversation(conversation_id)
            conversation = checked[conversation_id]
            if conversation is None or not may_use_slack_conversation(
                requester_id=self._requester_id, conversation=conversation
            ):
                continue
            try:
                reply_payload = await self._api(
                    "conversations.replies", channel=conversation_id, ts=root_ts, limit=15
                )
            except (ConnectorError, httpx.HTTPError):
                # Do not surface a case if the complete incident cannot be read.
                continue
            messages: list[dict[str, str]] = []
            for message in reply_payload.get("messages", []):
                text = message.get("text")
                timestamp = message.get("ts")
                if not isinstance(text, str) or not timestamp:
                    continue
                messages.append({"ts": str(timestamp), "text": text[:3000]})
            if not messages:
                continue
            messages.sort(key=lambda item: _slack_timestamp_number(item["ts"]))
            latest_ts = messages[-1]["ts"]
            next_cursor = reply_payload.get("response_metadata", {}).get("next_cursor")
            thread_may_be_truncated = bool(next_cursor) or len(messages) >= 15
            try:
                permalink = (
                    await self._api("chat.getPermalink", channel=conversation_id, message_ts=root_ts)
                ).get("permalink")
            except (ConnectorError, httpx.HTTPError):
                permalink = None
            thread_text = "\n".join(item["text"] for item in messages)[:12000]
            normalized_thread = thread_text.lower()
            focus_matches = [term for term in focus_terms if term in normalized_thread]
            focus_match_score = sum(focus_weights[term] for term in focus_matches)
            records.append(
                SourceRecord(
                    provider=Provider.SLACK,
                    source_id=f"{conversation_id}:{root_ts}",
                    title=candidate["channel_name"],
                    occurred_at=root_ts,
                    excerpt=thread_text,
                    source_url=permalink,
                    metadata={
                        "case_kind": "slack_thread",
                        "conversation_id": conversation_id,
                        "thread_ts": root_ts,
                        "latest_message_ts": latest_ts,
                        "thread_messages": messages,
                        "matched_queries": sorted(candidate["matched_queries"]),
                        "retrieval_score": priority(candidate)[:3],
                        "focus_matches": focus_matches,
                        "focus_match_count": len(focus_matches),
                        "focus_match_score": focus_match_score,
                        "thread_may_be_truncated": thread_may_be_truncated,
                        "search_may_be_incomplete": candidate["search_may_be_incomplete"],
                    },
                )
            )
        records.sort(
            key=lambda item: (
                int(item.metadata.get("focus_match_count", 0)) > 0,
                int(item.metadata.get("focus_match_score", 0)),
                int(item.metadata.get("focus_match_count", 0)),
                _slack_timestamp_number(item.metadata.get("latest_message_ts", item.occurred_at)),
                tuple(item.metadata.get("retrieval_score", (0, 0, 0))),
            ),
            reverse=True,
        )
        if not focus_terms:
            return records[:limit]
        comparable_records = [
            item
            for item in records
            if int(item.metadata.get("focus_match_count", 0)) >= minimum_focus_matches
        ]
        # Do not pad an answer with a company-only or feature-only hit when the
        # question names both. Returning no comparable case is more honest than
        # presenting an unrelated meeting as supporting evidence.
        return comparable_records[:limit]

    async def search(self, query: str, limit: int = 20) -> list[SourceRecord]:
        """Compatibility wrapper for callers that need Slack search evidence."""
        return await self.search_cases(query, limit=min(limit, 4))


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
