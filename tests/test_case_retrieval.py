from __future__ import annotations

import asyncio
import json

from app.connectors import (
    SlackReader,
    interpret_search_intent,
    slack_focus_terms,
    slack_minimum_focus_matches,
    slack_query_variants,
)
from app.models import Provider, SlackConversation, SourceRecord
from app.services import AnswerService, SearchOutcome, redact_sensitive_text


class FakeSlackReader(SlackReader):
    """In-memory Slack API fixture; no real workspace data is used in tests."""

    def __init__(self):
        self._requester_id = "U_REQUESTER"
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def close(self) -> None:
        return None

    async def _conversation(self, conversation_id: str) -> SlackConversation | None:
        if conversation_id == "G_PRIVATE":
            return SlackConversation(conversation_id, True, False, False, frozenset({"U_OTHER"}))
        return SlackConversation(conversation_id, False, False, False, frozenset())

    async def _api(self, method: str, **data):
        self.calls.append((method, data))
        if method == "search.messages":
            query = data["query"]
            if query == "전기요금":
                return {
                    "ok": True,
                    "messages": {
                        "matches": [
                            {"channel": {"id": "C_PUBLIC", "name": "public-billing"}, "ts": "1710000000.000100"},
                            {
                                "channel": {"id": "C_PUBLIC", "name": "public-billing"},
                                "ts": "1710000010.000200",
                                "thread_ts": "1710000000.000100",
                            },
                            {"channel": {"id": "G_PRIVATE", "name": "private"}, "ts": "1710001000.000100"},
                            {"channel": {"id": "D_APP", "name": "app-dm"}, "ts": "1710002000.000100"},
                        ]
                    },
                }
            if query == "전기요금 신청":
                return {
                    "ok": True,
                    "messages": {
                        "matches": [
                            {
                                "channel": {"id": "C_PUBLIC", "name": "public-billing"},
                                "ts": "1710000010.000200",
                                "thread_ts": "1710000000.000100",
                            }
                        ]
                    },
                }
            return {"ok": True, "messages": {"matches": []}}
        if method == "conversations.replies":
            assert data["channel"] == "C_PUBLIC"
            assert data["ts"] == "1710000000.000100"
            return {
                "ok": True,
                "messages": [
                    {"ts": "1710000010.000200", "text": "확인 후 등록완료 했습니다."},
                    {"ts": "1710000000.000100", "text": "전기요금 신청 후 화면에 표시되지 않습니다."},
                ],
            }
        if method == "chat.getPermalink":
            return {"ok": True, "permalink": f"https://slack.example/{data['channel']}/{data['message_ts']}"}
        raise AssertionError(f"unexpected API method: {method}")


class FocusSlackReader(SlackReader):
    """Fixture with one exact case and one company-only, unrelated case."""

    def __init__(self):
        self._requester_id = "U_REQUESTER"

    async def close(self) -> None:
        return None

    async def _conversation(self, conversation_id: str) -> SlackConversation | None:
        return SlackConversation(conversation_id, False, False, False, frozenset())

    async def _api(self, method: str, **data):
        if method == "search.messages":
            query = data["query"]
            exact = {
                "channel": {"id": "C_EXACT", "name": "meter"},
                "ts": "1710000000.000100",
            }
            company_only = {
                "channel": {"id": "C_COMPANY", "name": "meeting"},
                "ts": "1710001000.000100",
            }
            matches = {
                "서울도시가스 검침일자": [exact],
                "검침일자": [exact],
                "서울도시가스": [exact, company_only],
                "도시가스 서울도시가스": [company_only],
            }.get(query, [])
            return {"ok": True, "messages": {"matches": matches}}
        if method == "conversations.replies":
            message = (
                "서울도시가스 검침일자 변경에 따른 자가검침 예정일자 조정"
                if data["channel"] == "C_EXACT"
                else "서울도시가스 계약 전환 및 개발 일정 회의"
            )
            return {"ok": True, "messages": [{"ts": data["ts"], "text": message}]}
        if method == "chat.getPermalink":
            return {"ok": True, "permalink": f"https://slack.example/{data['channel']}/{data['message_ts']}"}
        raise AssertionError(f"unexpected API method: {method}")


def _case(source_id: str, thread_messages: list[str], *, source_url: str | None = "https://slack.example/source"):
    return SourceRecord(
        provider=Provider.SLACK,
        source_id=source_id,
        title="not sent to model",
        occurred_at="1710000000.000100",
        excerpt="\n".join(thread_messages),
        source_url=source_url,
        metadata={
            "case_kind": "slack_thread",
            "thread_messages": [
                {"ts": f"171000000{i}.000100", "text": text} for i, text in enumerate(thread_messages)
            ],
        },
    )


def test_korean_query_planner_is_bounded_and_expands_registration_complaint():
    queries = slack_query_variants("전기요금 신청했는데 아직 정상적으로 이용이 안 된다는 민원이 들어왔어. 왜 그런건지 알려줘")

    assert queries == ["전기요금", "전기요금 신청", "전기요금 등록", "전기요금 조회"]
    assert len(queries) <= 4
    assert all("알려" not in query for query in queries)


def test_query_planner_prioritizes_company_feature_and_input_symptom():
    queries = slack_query_variants("서울도시가스 고객이 자가검침 기간인데 입력이 안된다는 민원을 보냈어")

    assert queries == ["서울도시가스 자가검침", "서울도시가스 입력", "자가검침 입력", "서울도시가스"]
    assert "도시가스" not in queries


def test_query_planner_prioritizes_incident_words_over_conversational_words():
    question = "서울도시가스에서 이번에 또 미납요금 확정 작업 전에 미납요금이 노출됐대. 히스토리 보여줘."

    assert slack_focus_terms(question) == ["서울도시가스", "미납", "확정", "노출"]
    assert slack_query_variants(question) == [
        "서울도시가스 미납",
        "서울도시가스 확정",
        "미납 확정",
        "미납 노출",
    ]


def test_search_intent_keeps_time_words_as_context_not_search_terms():
    intent = interpret_search_intent(
        "서울도시가스에서 이번에 또 미납요금 확정 작업 전에 미납요금이 노출됐대. 히스토리 보여줘"
    )

    assert intent.subject == "서울도시가스"
    assert intent.features == ("미납", "확정", "노출")
    assert intent.request_type == "history"
    assert intent.time_context == "이번"
    assert intent.clarification_prompt is None


def test_search_intent_asks_for_clarification_when_company_has_no_incident():
    intent = interpret_search_intent("서울도시가스 문제 히스토리 보여줘")

    assert intent.subject == "서울도시가스"
    assert not intent.features
    assert intent.clarification_prompt is not None
    assert "기능이나 현상" in intent.clarification_prompt


def test_named_company_question_requires_company_and_feature_match():
    question = "서울도시가스에서 검침일자 변경을 해야 된대. 쿼리가 뭐였지?"
    reader = FocusSlackReader()

    records = asyncio.run(reader.search_cases(question))

    assert slack_minimum_focus_matches(question) == 2
    assert [record.source_id for record in records] == ["C_EXACT:1710000000.000100"]


def test_slack_hits_in_one_thread_become_one_case_and_current_dm_is_excluded():
    reader = FakeSlackReader()

    records = asyncio.run(
        reader.search_cases(
            "전기요금 신청했는데 아직 정상적으로 이용이 안 돼요",
            exclude_conversation_id="D_APP",
        )
    )

    assert len(records) == 1
    case = records[0]
    assert case.source_id == "C_PUBLIC:1710000000.000100"
    assert case.source_url == "https://slack.example/C_PUBLIC/1710000000.000100"
    assert [message["ts"] for message in case.metadata["thread_messages"]] == [
        "1710000000.000100",
        "1710000010.000200",
    ]
    assert set(case.metadata["matched_queries"]) == {"전기요금", "전기요금 신청"}
    assert all("G_PRIVATE" not in record.source_id for record in records)
    assert all("D_APP" not in record.source_id for record in records)
    # Four bounded searches, then only one permission check/thread read for the
    # one distinct accessible candidate in this fixture.
    assert len([call for call in reader.calls if call[0] == "search.messages"]) == 4
    assert len([call for call in reader.calls if call[0] == "conversations.replies"]) == 1


def test_evidence_fallback_redacts_sensitive_values_and_does_not_overclaim():
    case = _case(
        "C_PUBLIC:1710000000.000100",
        [
            "고객명: 홍길동\n고객번호: 1234567890\n연락처: 010-1234-5678\n이메일: user@example.com\n주소: 서울시 어딘가",
            "외부 정보 오류를 확인했고 등록완료 했습니다.",
        ],
        source_url="https://safescg.slack.com/archives/C_PUBLIC/p1710000000000100",
    )
    outcome = SearchOutcome(records=[case], unavailable=[])

    answer = AnswerService._evidence_only(outcome, provider_problem=True)
    model_case = AnswerService._model_case(1, case)
    model_text = json.dumps(model_case, ensure_ascii=False)

    for secret in ("홍길동", "1234567890", "010-1234-5678", "user@example.com", "서울시 어딘가"):
        assert secret not in answer
        assert secret not in model_text
    assert "기록상 완료 여부: 완료로 표현된 후속 기록이 있습니다." in answer
    assert "<https://safescg.slack.com/archives/C_PUBLIC/p1710000000000100|원문>" in answer
    assert "safescg.slack.com" not in model_text


def test_evidence_fallback_exposes_only_safe_model_failure_diagnostic():
    case = _case("C_PUBLIC:1710000000.000100", ["자가검침 입력 오류", "확인 중입니다."])

    answer = AnswerService._evidence_only(
        SearchOutcome(records=[case], unavailable=[]),
        provider_problem=True,
        provider_diagnostic="HTTP 429",
    )

    assert "진단 코드: HTTP 429" in answer


def test_query_lookup_fallback_is_concise_and_never_invents_missing_sql():
    case = _case(
        "C_PUBLIC:1710000000.000100",
        ["서울도시가스 검침일자 변경에 따른 자가검침 예정일자 변경이 필요합니다. 조회 쿼리 공유 요청."],
    )

    answer = AnswerService._evidence_only(
        SearchOutcome(records=[case], unavailable=[]),
        question="서울도시가스 검침일자 변경 쿼리가 뭐였지?",
        provider_problem=True,
        provider_diagnostic="HTTP 429",
    )

    assert "기록상 실제 쿼리 전문은 확인되지 않습니다." in answer
    assert "가장 가까운 기록" in answer
    assert "사례 2" not in answer
    assert "AI 요약은 현재 사용할 수 없습니다 (HTTP 429)." in answer


def test_in_progress_record_is_not_presented_as_completed():
    case = _case("C_PUBLIC:1710000000.000100", ["청구서가 보이지 않습니다.", "현재 확인 중입니다."])

    answer = AnswerService._evidence_only(SearchOutcome(records=[case], unavailable=[]))

    assert "진행·확인 중으로 표현된 기록이 있습니다." in answer
    assert "완료로 표현된 후속 기록" not in answer


def test_answer_uses_at_most_four_distinct_slack_cases():
    cases = [_case(f"C_PUBLIC:{index}", [f"사례 {index}"]) for index in range(5)]

    selected = AnswerService._answer_records(cases)

    assert [case.source_id for case in selected] == [f"C_PUBLIC:{index}" for index in range(4)]

    external = SourceRecord(
        provider=Provider.JIRA,
        source_id="JIRA-1",
        title="external",
        occurred_at=None,
        excerpt="external record",
        source_url=None,
    )
    assert len(AnswerService._answer_records(cases[:4] + [external])) == 4


def test_answer_drops_broad_slack_cases_when_focused_case_exists():
    focused = _case("C_PUBLIC:focused", ["서울도시가스 자가검침 입력 불가"])
    focused.metadata["focus_match_count"] = 2
    broad = _case("C_PUBLIC:broad", ["도시가스 계약 관련 회의"])

    selected = AnswerService._answer_records([broad, focused])

    assert [case.source_id for case in selected] == ["C_PUBLIC:focused"]


def test_redactor_handles_unlabelled_phone_identifier_name_and_address():
    redacted = redact_sensitive_text("홍길동님 문의 번호 123456789, 연락은 010 3333 4444, 서울시 강남구 테헤란로 123")

    assert "홍길동" not in redacted
    assert "123456789" not in redacted
    assert "010 3333 4444" not in redacted
    assert "서울시 강남구 테헤란로 123" not in redacted
