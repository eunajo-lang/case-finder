from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


class SecurityError(ValueError):
    pass


def verify_slack_signature(*, signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - request_time) > 60 * 5:
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@dataclass(frozen=True)
class OAuthState:
    provider: str
    slack_user_id: str
    nonce: str
    expires_at: int


class StateSigner:
    """A signed, short-lived state value. The nonce is consumed by the database."""

    def __init__(self, secret: str):
        self._secret = secret.encode("utf-8")

    def issue(self, *, provider: str, slack_user_id: str, ttl_seconds: int = 600) -> tuple[str, OAuthState]:
        state = OAuthState(
            provider=provider,
            slack_user_id=slack_user_id,
            nonce=secrets.token_urlsafe(24),
            expires_at=int(time.time()) + ttl_seconds,
        )
        payload = json.dumps(state.__dict__, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii"), state

    def verify(self, value: str, expected_provider: str) -> OAuthState:
        try:
            encoded, supplied_signature = value.encode("ascii").split(b".", 1)
            expected_signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
            padding = b"=" * (-len(supplied_signature) % 4)
            if not hmac.compare_digest(expected_signature, base64.urlsafe_b64decode(supplied_signature + padding)):
                raise SecurityError("state signature mismatch")
            payload_padding = b"=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + payload_padding))
            state = OAuthState(**payload)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeEncodeError) as error:
            raise SecurityError("invalid OAuth state") from error
        if state.provider != expected_provider or state.expires_at < int(time.time()):
            raise SecurityError("expired or mismatched OAuth state")
        return state


def state_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
