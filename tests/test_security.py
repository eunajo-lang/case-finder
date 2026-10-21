import hashlib
import hmac
import time

import pytest

from app.security import SecurityError, StateSigner, verify_slack_signature


def test_valid_slack_signature_is_accepted():
    secret = "signing-secret"
    timestamp = str(int(time.time()))
    body = b'{"type":"url_verification"}'
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(
        signing_secret=secret, timestamp=timestamp, body=body, signature=signature
    )


def test_old_or_tampered_slack_signature_is_rejected():
    assert not verify_slack_signature(
        signing_secret="secret", timestamp="1", body=b"x", signature="v0=not-a-signature"
    )


def test_oauth_state_is_provider_bound_and_signed():
    signer = StateSigner("local-test-secret")
    value, issued = signer.issue(provider="gmail", slack_user_id="U_TEST")
    assert signer.verify(value, "gmail") == issued
    with pytest.raises(SecurityError):
        signer.verify(value, "jira")
    tampered = ("A" if value[0] != "A" else "B") + value[1:]
    with pytest.raises(SecurityError):
        signer.verify(tampered, "gmail")
