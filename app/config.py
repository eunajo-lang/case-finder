from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _required(name: str) -> str:
    value = _optional(name)
    if not value:
        raise ConfigurationError(f"필수 환경 변수가 없습니다: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    database_path: Path
    storage_backend: str
    firestore_collection_prefix: str
    token_encryption_key: str
    state_signing_secret: str
    slack_signing_secret: str
    slack_bot_token: str
    slack_client_id: str | None
    slack_client_secret: str | None
    jira_client_id: str | None
    jira_client_secret: str | None
    jira_cloud_id: str | None
    gmail_client_id: str | None
    gmail_client_secret: str | None
    openai_api_key: str | None
    openai_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = _required("APP_BASE_URL").rstrip("/")
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ConfigurationError("APP_BASE_URL은 HTTPS 주소여야 합니다. 로컬 테스트만 127.0.0.1을 허용합니다.")
        key = _required("TOKEN_ENCRYPTION_KEY")
        try:
            decoded = base64.urlsafe_b64decode(key.encode("ascii"))
        except Exception as error:  # pragma: no cover - Fernet will also validate this
            raise ConfigurationError("TOKEN_ENCRYPTION_KEY 형식이 올바르지 않습니다.") from error
        if len(decoded) != 32:
            raise ConfigurationError("TOKEN_ENCRYPTION_KEY는 Fernet의 32바이트 키여야 합니다.")
        return cls(
            app_base_url=base_url,
            database_path=Path(os.getenv("DATABASE_PATH", "data/case_finder.sqlite3")),
            storage_backend=os.getenv("STORAGE_BACKEND", "sqlite").lower(),
            firestore_collection_prefix=os.getenv("FIRESTORE_COLLECTION_PREFIX", "case_finder"),
            token_encryption_key=key,
            state_signing_secret=_required("STATE_SIGNING_SECRET"),
            slack_signing_secret=_required("SLACK_SIGNING_SECRET"),
            slack_bot_token=_required("SLACK_BOT_TOKEN"),
            slack_client_id=_optional("SLACK_CLIENT_ID"),
            slack_client_secret=_optional("SLACK_CLIENT_SECRET"),
            jira_client_id=_optional("JIRA_CLIENT_ID"),
            jira_client_secret=_optional("JIRA_CLIENT_SECRET"),
            jira_cloud_id=_optional("JIRA_CLOUD_ID"),
            gmail_client_id=_optional("GMAIL_CLIENT_ID"),
            gmail_client_secret=_optional("GMAIL_CLIENT_SECRET"),
            openai_api_key=_optional("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        )

    def callback_url(self, provider: str) -> str:
        return f"{self.app_base_url}/oauth/{provider}/callback"

    def require_provider_credentials(self, provider: str) -> tuple[str, str]:
        credentials = {
            "slack": (self.slack_client_id, self.slack_client_secret),
            "jira": (self.jira_client_id, self.jira_client_secret),
            "gmail": (self.gmail_client_id, self.gmail_client_secret),
        }.get(provider)
        if credentials is None or not all(credentials):
            raise ConfigurationError(f"{provider} OAuth 클라이언트 정보를 아직 설정하지 않았습니다.")
        return credentials[0], credentials[1]
