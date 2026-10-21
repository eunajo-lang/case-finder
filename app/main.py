from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import ConfigurationError, Settings
from .models import Provider
from .oauth import OAuthClient, OAuthError
from .security import SecurityError, StateSigner, state_digest, verify_slack_signature
from .services import AnswerService, SearchService
from .store import build_store


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Case Finder", docs_url=None, redoc_url=None)
    store = build_store(settings)
    signer = StateSigner(settings.state_signing_secret)
    oauth = OAuthClient(settings)
    search = SearchService(settings, store)
    answer = AnswerService(settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/slack/events")
    async def slack_events(request: Request, tasks: BackgroundTasks) -> JSONResponse:
        body = await request.body()
        if not verify_slack_signature(
            signing_secret=settings.slack_signing_secret,
            timestamp=request.headers.get("x-slack-request-timestamp", ""),
            body=body,
            signature=request.headers.get("x-slack-signature", ""),
        ):
            raise HTTPException(status_code=401, detail="invalid Slack signature")
        payload = json.loads(body)
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload["challenge"]})
        if payload.get("type") != "event_callback":
            return JSONResponse({"ok": True})
        event_id = payload.get("event_id")
        event = payload.get("event", {})
        if not event_id or not store.claim_event(event_id):
            return JSONResponse({"ok": True})
        is_direct_message = event.get("type") == "message" and event.get("channel_type") == "im"
        is_channel_mention = event.get("type") == "app_mention"
        if (is_direct_message or is_channel_mention) and not event.get("bot_id"):
            user_id, channel_id, text = event.get("user"), event.get("channel"), event.get("text", "").strip()
            if is_channel_mention:
                text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            if user_id and channel_id and text:
                tasks.add_task(
                    handle_question,
                    settings,
                    store,
                    signer,
                    oauth,
                    search,
                    answer,
                    user_id,
                    channel_id,
                    text,
                    event,
                    channel_id if is_channel_mention else None,
                )
        return JSONResponse({"ok": True})

    @app.get("/oauth/{provider}/callback", response_class=HTMLResponse)
    async def oauth_callback(
        provider: Provider, state: str, code: Optional[str] = None, error: Optional[str] = None
    ) -> HTMLResponse:
        try:
            signed_state = signer.verify(state, provider.value)
            valid = store.consume_oauth_state(
                digest=state_digest(state), provider=provider, slack_user_id=signed_state.slack_user_id
            )
            if not valid:
                raise SecurityError("OAuth state has already been used")
            if error or not code:
                raise OAuthError("연결이 취소되었거나 승인되지 않았습니다.")
            token_bundle, scopes = await oauth.exchange_code(provider, code)
            store.save_token_bundle(
                provider=provider, slack_user_id=signed_state.slack_user_id, bundle=token_bundle, scopes=scopes
            )
            return HTMLResponse("<h2>연결되었습니다.</h2><p>이 창을 닫고 Slack 개인 대화로 돌아가 질문하세요.</p>")
        except (SecurityError, OAuthError, ConfigurationError, httpx.HTTPError):
            return HTMLResponse(
                "<h2>연결하지 못했습니다.</h2><p>Slack으로 돌아가 다시 시도하거나 관리자에게 문의하세요.</p>",
                status_code=400,
            )

    return app


async def handle_question(
    settings: Settings,
    store: ConnectionStore,
    signer: StateSigner,
    oauth: OAuthClient,
    search: SearchService,
    answer: AnswerService,
    slack_user_id: str,
    channel_id: str,
    text: str,
    event: dict[str, Any],
    source_conversation_id: str | None = None,
) -> None:
    connected = store.connected_providers(slack_user_id)
    if not connected:
        message = _connection_message(settings, store, signer, oauth, slack_user_id, list(Provider))
    else:
        try:
            outcome = await search.search(
                slack_user_id=slack_user_id,
                question=text,
                slack_conversation_id=source_conversation_id,
                exclude_slack_message_ts=event.get("ts"),
            )
            message = await answer.compose(question=text, outcome=outcome)
        except Exception:
            # Do not include provider error text: it might expose source or configuration details.
            message = "기록을 안전하게 조회하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요."
    await _post_to_slack(
        bot_token=settings.slack_bot_token,
        channel_id=channel_id,
        text=message,
        thread_ts=event.get("thread_ts") or event.get("ts"),
    )


def _connection_message(
    settings: Settings,
    store: ConnectionStore,
    signer: StateSigner,
    oauth: OAuthClient,
    slack_user_id: str,
    missing: list[Provider],
) -> str:
    links: list[str] = []
    labels = {Provider.SLACK: "Slack 기록", Provider.JIRA: "Jira", Provider.GMAIL: "Gmail"}
    for provider in missing:
        try:
            state_value, state = signer.issue(provider=provider.value, slack_user_id=slack_user_id)
            store.save_oauth_state(
                digest=state_digest(state_value),
                provider=provider,
                slack_user_id=slack_user_id,
                expires_at=state.expires_at,
            )
            url = oauth.authorization_url(provider, state_value)
            links.append(f"<{url}|{labels[provider]} 연결>")
        except ConfigurationError:
            links.append(f"{labels[provider]}: 관리자 설정 필요")
    return "기록을 권한에 맞게 검색하려면 먼저 개인 연결이 필요합니다. " + " · ".join(links)


async def _post_to_slack(*, bot_token: str, channel_id: str, text: str, thread_ts: str | None) -> None:
    payload: dict[str, str] = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json=payload,
        )


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env())
