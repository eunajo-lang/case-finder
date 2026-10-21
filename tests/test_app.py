import hashlib
import hmac
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def settings(tmp_path):
    return Settings(
        app_base_url="http://127.0.0.1:8000",
        database_path=tmp_path / "case-finder.sqlite3",
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
