from cryptography.fernet import Fernet

from app.models import Provider
from app.store import ConnectionStore


def test_token_is_encrypted_at_rest_and_retrievable(tmp_path):
    token = "secret-token-that-must-not-appear-in-sqlite"
    database = tmp_path / "case-finder.sqlite3"
    store = ConnectionStore(database, Fernet.generate_key().decode())
    store.save_connection(provider=Provider.GMAIL, slack_user_id="U_TEST", access_token=token)

    assert store.access_token(provider=Provider.GMAIL, slack_user_id="U_TEST") == token
    assert token.encode() not in database.read_bytes()


def test_oauth_state_is_one_time_use(tmp_path):
    store = ConnectionStore(tmp_path / "case-finder.sqlite3", Fernet.generate_key().decode())
    store.save_oauth_state(digest="test", provider=Provider.JIRA, slack_user_id="U_TEST", expires_at=2_000_000_000)
    assert store.consume_oauth_state(digest="test", provider=Provider.JIRA, slack_user_id="U_TEST")
    assert not store.consume_oauth_state(digest="test", provider=Provider.JIRA, slack_user_id="U_TEST")


def test_pending_question_is_encrypted_and_one_time(tmp_path):
    database = tmp_path / "case-finder.sqlite3"
    store = ConnectionStore(database, Fernet.generate_key().decode())
    question = "서울도시가스에서 어떤 현상인지 확인 중"

    store.save_pending_question(
        slack_user_id="U_TEST", channel_id="D_TEST", question=question, expires_at=2_000_000_000
    )

    assert question.encode() not in database.read_bytes()
    assert store.consume_pending_question(slack_user_id="U_TEST", channel_id="D_TEST") == question
    assert store.consume_pending_question(slack_user_id="U_TEST", channel_id="D_TEST") is None
