from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache(maxsize=None)
def _read_secret(name: str) -> str:
    """Read a deployment secret without placing its value in app.yaml.

    Local and Cloud Run deployments provide the value directly as an
    environment variable. App Engine supplies a Secret Manager secret name in
    ``<VARIABLE>_SECRET`` instead, so its default service account can retrieve
    the value at runtime.
    """
    project_id = _optional("GCP_PROJECT_ID") or _optional("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise ConfigurationError(f"{name}_SECRET를 사용하려면 GCP_PROJECT_ID가 필요합니다.")
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(
            request={"name": f"projects/{project_id}/secrets/{name}/versions/latest"}
        )
        return response.payload.data.decode("utf-8").strip()
    except Exception as error:
        raise ConfigurationError(f"Secret Manager에서 {name}을(를) 읽을 수 없습니다.") from error


def _value_or_secret(variable: str, *, required: bool = False) -> str | None:
    value = _optional(variable)
    if value:
        return value
    secret_name = _optional(f"{variable}_SECRET")
    if secret_name:
        return _read_secret(secret_name)
    if required:
        raise ConfigurationError(f"필수 환경 변수가 없습니다: {variable}")
    return None


def _service_account_json() -> str | None:
    """Decode an optional Render-only GCP service-account credential.

    Render stores the base64 form as an encrypted environment variable.  The
    raw key is never committed, logged, or written to disk by this app.
    """
    encoded = _optional("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict) or parsed.get("type") != "service_account":
            raise ValueError("not a service account")
        return decoded
    except Exception as error:
        raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 형식이 올바르지 않습니다.") from error


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
    google_service_account_json: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = _required("APP_BASE_URL").rstrip("/")
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ConfigurationError("APP_BASE_URL은 HTTPS 주소여야 합니다. 로컬 테스트만 127.0.0.1을 허용합니다.")
        key = _value_or_secret("TOKEN_ENCRYPTION_KEY", required=True)
        assert key is not None
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
            state_signing_secret=_value_or_secret("STATE_SIGNING_SECRET", required=True) or "",
            slack_signing_secret=_value_or_secret("SLACK_SIGNING_SECRET", required=True) or "",
            slack_bot_token=_value_or_secret("SLACK_BOT_TOKEN", required=True) or "",
            slack_client_id=_value_or_secret("SLACK_CLIENT_ID"),
            slack_client_secret=_value_or_secret("SLACK_CLIENT_SECRET"),
            jira_client_id=_value_or_secret("JIRA_CLIENT_ID"),
            jira_client_secret=_value_or_secret("JIRA_CLIENT_SECRET"),
            jira_cloud_id=_value_or_secret("JIRA_CLOUD_ID"),
            gmail_client_id=_value_or_secret("GMAIL_CLIENT_ID"),
            gmail_client_secret=_value_or_secret("GMAIL_CLIENT_SECRET"),
            openai_api_key=_value_or_secret("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            google_service_account_json=_service_account_json(),
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
