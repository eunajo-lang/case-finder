from __future__ import annotations

from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import Provider


class OAuthError(RuntimeError):
    pass


SLACK_USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "search:read",
    "users:read",
]


class OAuthClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def authorization_url(self, provider: Provider, state: str) -> str:
        client_id, _ = self.settings.require_provider_credentials(provider.value)
        callback = self.settings.callback_url(provider.value)
        if provider is Provider.SLACK:
            params = {
                "client_id": client_id,
                "redirect_uri": callback,
                "user_scope": ",".join(SLACK_USER_SCOPES),
            }
            return "https://slack.com/oauth/v2/authorize?" + urlencode({**params, "state": state})
        if provider is Provider.JIRA:
            params = {
                "audience": "api.atlassian.com",
                "client_id": client_id,
                "scope": "read:jira-work offline_access",
                "redirect_uri": callback,
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
            return "https://auth.atlassian.com/authorize?" + urlencode(params)
        if provider is Provider.GMAIL:
            params = {
                "client_id": client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        raise OAuthError("지원하지 않는 연결입니다.")

    async def exchange_code(self, provider: Provider, code: str) -> tuple[dict[str, object], list[str]]:
        client_id, client_secret = self.settings.require_provider_credentials(provider.value)
        redirect_uri = self.settings.callback_url(provider.value)
        async with httpx.AsyncClient(timeout=20) as client:
            if provider is Provider.SLACK:
                response = await client.post(
                    "https://slack.com/api/oauth.v2.access",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                user = payload.get("authed_user", {})
                if not payload.get("ok") or not user.get("access_token"):
                    raise OAuthError("Slack 사용자 연결에 실패했습니다.")
                return (
                    {"access_token": user["access_token"], "refresh_token": user.get("refresh_token")},
                    str(user.get("scope", "")).split(","),
                )
            if provider is Provider.JIRA:
                response = await client.post(
                    "https://auth.atlassian.com/oauth/token",
                    json={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                )
            elif provider is Provider.GMAIL:
                response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                )
            else:  # pragma: no cover
                raise OAuthError("지원하지 않는 연결입니다.")
            response.raise_for_status()
            payload = response.json()
            if not payload.get("access_token"):
                raise OAuthError(f"{provider.value} 사용자 연결에 실패했습니다.")
            if provider is Provider.JIRA:
                resources = await client.get(
                    "https://api.atlassian.com/oauth/token/accessible-resources",
                    headers={"Authorization": f"Bearer {payload['access_token']}"},
                )
                resources.raise_for_status()
                available = resources.json()
                configured = self.settings.jira_cloud_id
                selected = next((item for item in available if item.get("id") == configured), None)
                selected = selected or next(
                    (item for item in available if item.get("url") == "https://scglab.atlassian.net"), None
                )
                if selected is None:
                    raise OAuthError("접근 가능한 Jira 사이트를 확인하지 못했습니다.")
                payload["cloud_id"] = selected["id"]
            scopes = str(payload.get("scope", "")).replace(",", " ").split()
            return payload, scopes
