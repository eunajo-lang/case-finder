from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import Settings
from .connectors import ConnectorError, GmailReader, JiraReader, SlackReader
from .models import Provider, SourceRecord
from .store import ConnectionStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchOutcome:
    records: list[SourceRecord]
    unavailable: list[Provider]


class SearchService:
    def __init__(self, settings: Settings, store: ConnectionStore):
        self.settings = settings
        self.store = store

    async def search(self, *, slack_user_id: str, question: str) -> SearchOutcome:
        tasks = []
        active: list[tuple[Provider, object]] = []
        slack_token = self.store.access_token(provider=Provider.SLACK, slack_user_id=slack_user_id)
        if slack_token:
            reader = SlackReader(slack_token, slack_user_id)
            active.append((Provider.SLACK, reader))
            tasks.append(reader.search(question))
        jira_token = self.store.access_token(provider=Provider.JIRA, slack_user_id=slack_user_id)
        jira_bundle = self.store.token_bundle(provider=Provider.JIRA, slack_user_id=slack_user_id) or {}
        jira_cloud_id = jira_bundle.get("cloud_id") or self.settings.jira_cloud_id
        if jira_token and isinstance(jira_cloud_id, str):
            reader = JiraReader(jira_token, jira_cloud_id)
            active.append((Provider.JIRA, reader))
            tasks.append(reader.search(question))
        gmail_token = self.store.access_token(provider=Provider.GMAIL, slack_user_id=slack_user_id)
        if gmail_token:
            reader = GmailReader(gmail_token)
            active.append((Provider.GMAIL, reader))
            tasks.append(reader.search(question))

        unavailable = [provider for provider in Provider if provider not in [item[0] for item in active]]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await asyncio.gather(*(reader.close() for _, reader in active), return_exceptions=True)

        records: list[SourceRecord] = []
        for (provider, _), result in zip(active, results):
            if isinstance(result, Exception):
                unavailable.append(provider)
                continue
            records.extend(result)
        # Newer records first; unknown dates last. Do not merge records into a single incident here.
        records.sort(key=lambda item: item.occurred_at or "", reverse=True)
        return SearchOutcome(records=records[:30], unavailable=list(dict.fromkeys(unavailable)))


class AnswerService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def compose(self, *, question: str, outcome: SearchOutcome) -> str:
        if not outcome.records:
            return self._no_records(outcome.unavailable)
        if not self.settings.openai_api_key:
            return self._evidence_only(outcome)
        try:
            return await self._openai_answer(question=question, records=outcome.records, unavailable=outcome.unavailable)
        except httpx.HTTPStatusError as error:
            logger.warning("OpenAI summary request failed with HTTP %s", error.response.status_code)
            return self._evidence_only(outcome, provider_problem=True)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            logger.warning("OpenAI summary request failed: %s", type(error).__name__)
            # Fail safely: provide only directly observed source labels, never a made-up conclusion.
            return self._evidence_only(outcome, provider_problem=True)

    @staticmethod
    def _no_records(unavailable: list[Provider]) -> str:
        connected = "" if not unavailable else f" 연결되지 않았거나 조회할 수 없는 자료: {', '.join(item.value for item in unavailable)}."
        return "접근 가능한 기록에서 관련 후보를 찾지 못했습니다." + connected + " 기록이 없었다고 단정할 수는 없습니다."

    @staticmethod
    def _evidence_only(outcome: SearchOutcome, provider_problem: bool = False) -> str:
        lines = ["*핵심 결론*", "관련 후보는 찾았지만, 근거만으로 처리 결과나 최종 결론을 단정할 수 없습니다."]
        lines.extend(["", "*기록에서 마지막으로 확인된 상태*"])
        newest = outcome.records[0]
        newest_date = newest.occurred_at or "날짜 미확인"
        lines.append(
            f"가장 최근 후보는 {newest_date}의 [{newest.provider.value}] {newest.title}입니다. "
            "이 기록이 최종 상태인지는 확인되지 않았습니다."
        )
        lines.extend(["", "*근거*"])
        if provider_problem:
            lines.append("AI 요약 서비스를 사용할 수 없어, 아래 원문 후보만 제공합니다.")
        for index, item in enumerate(outcome.records[:8], 1):
            date = item.occurred_at or "날짜 미확인"
            link = f" <{item.source_url}|원문>" if item.source_url else " (원문 링크는 구현 검증 필요)"
            lines.append(f"{index}. [{item.provider.value}] {item.title} · {date}{link}")
        lines.extend(["", "*확인 필요*", "서로 다른 사건 후보가 섞였을 수 있으므로, 처리 결과는 원문을 확인한 뒤 판단해야 합니다."])
        return "\n".join(lines)

    async def _openai_answer(
        self, *, question: str, records: list[SourceRecord], unavailable: list[Provider]
    ) -> str:
        sources = []
        for index, item in enumerate(records[:12], 1):
            sources.append(
                {
                    "id": index,
                    "provider": item.provider.value,
                    "date": item.occurred_at,
                    "title": item.title,
                    "excerpt": item.excerpt[:4000],
                    "url": item.source_url,
                }
            )
        instruction = """당신은 업무 기록을 설명하는 한국어 비서다. 제공된 근거만 사용한다.
추측, 일반 지식, 사용자 질문의 주장만으로 사실을 만들지 않는다. 서로 다른 사건을 합치지 않는다.

답변은 짧고 반드시 아래 순서를 따른다.
*핵심 결론*
*기록에서 마지막으로 확인된 상태*
*근거*
*확인 필요* (근거 부족·충돌·여러 사건 후보가 있을 때만)

규칙:
- 사실, 검토 의견, 제안, 최종 결정, 실행 결과, 이후 변경을 구분한다.
- 마지막 상태는 반드시 '기록에서 마지막으로 확인된 상태'라고 표현하며, 최신 메시지 하나만으로 최종 결정이라 단정하지 않는다.
- 사건이 충분히 특정되지 않거나 후보가 서로 다르면 결론을 내리지 말고 시기·증상·처리 결과 중 무엇을 알려주면 구분할 수 있는지 한 가지 질문을 한다.
- 각 주요 판단에 [근거 N]을 붙이고, 근거 목록에는 해당 번호와 제공된 URL만 쓴다. URL·날짜·처리 결과를 지어내지 않는다.
- 기록을 찾지 못해도 '없었다'고 단정하지 않는다.
"""
        payload = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": instruction}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"질문: {question}\n조회 불가 연동: {[item.value for item in unavailable]}\n근거: {sources}",
                        }
                    ],
                },
            ],
        }
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            output = response.json().get("output", [])
        texts = [
            part.get("text", "")
            for item in output
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        answer = "\n".join(texts).strip()
        if not answer:
            raise ValueError("empty model response")
        return answer


def display_date(value: str | None) -> str:
    if not value:
        return "날짜 미확인"
    if value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    return value
