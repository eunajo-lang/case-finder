import hashlib
import hmac
import time
import asyncio

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app import main
from app.main import create_app
from app.models import Provider
from app.services import SearchOutcome


def settings(tmp_path):
    return Settings(
        app_base_url="http://127.0.0.1:8000",
        database_path=tmp_path / "case-finder.sqlite3",
        storage_backend="sqlite",
        firestore_collection_prefix="case_finder",
        token_encryption_key=Fernet.generate_key().decode(),
        state_signing_secret="state-secret",
        slack_signing_secret="slack-secret",
        slack_bot_token="xoxb-test",
        slack_client_id=None,
        slack_client_secret=None,
        jira_client_id=None,
        jira_client_secret=None,
        jira_cloud_id=None,
        gmail_client_id=None,
        gmail_client_secret=None,
        openai_api_key=None,
        openai_model="test-model",
    )


def test_healthz(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_slack_url_verification_requires_signature(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    body = b'{"type":"url_verification","challenge":"safe-challenge"}'
    timestamp = str(int(time.time()))
    signed = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(b"slack-secret", signed, hashlib.sha256).hexdigest()

    response = client.post(
        "/slack/events",
        content=body,
        headers={"x-slack-request-timestamp": timestamp, "x-slack-signature": signature},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "safe-challenge"}
    assert client.post("/slack/events", content=body).status_code == 401


class _ClarificationStore:
    def __init__(self):
        self.pending: str | None = None

    def connected_providers(self, _slack_user_id):
        return {Provider.SLACK}

    def consume_pending_question(self, **_kwargs):
        result, self.pending = self.pending, None
        return result

    def save_pending_question(self, *, question, **_kwargs):
        self.pending = question


class _Search:
    def __init__(self):
        self.questions: list[str] = []

    async def search(self, *, question, **_kwargs):
        self.questions.append(question)
        return SearchOutcome(records=[], unavailable=[])


class _Answer:
    async def compose(self, *, question, outcome):
        return f"searched: {question}"


def test_dm_clarification_is_requested_then_combined_before_search(tmp_path, monkeypatch):
    store = _ClarificationStore()
    search = _Search()
    sent: list[str] = []

    async def post_to_slack(*, text, **_kwargs):
        sent.append(text)

    monkeypatch.setattr(main, "_post_to_slack", post_to_slack)
    kwargs = {
        "settings": settings(tmp_path),
        "store": store,
        "signer": None,
        "oauth": None,
        "search": search,
        "answer": _Answer(),
        "slack_user_id": "U_TEST",
        "channel_id": "D_TEST",
        "event": {"ts": "1.0"},
    }

    asyncio.run(main.handle_question(text="서울도시가스 문제 히스토리 보여줘", **kwargs))
    assert not search.questions
    assert "기능이나 현상" in sent[-1]

    asyncio.run(main.handle_question(text="미납요금 확정 전에 노출됐어", **kwargs))
    assert search.questions == [
        "서울도시가스 문제 히스토리 보여줘\n추가 설명: 미납요금 확정 전에 노출됐어"
    ]
    assert sent[-1].startswith("searched:")
