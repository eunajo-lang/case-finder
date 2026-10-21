from __future__ import annotations

import json
import sqlite3
import time
import base64
from pathlib import Path

from cryptography.fernet import Fernet

from .models import Provider


def build_store(settings):
    if settings.storage_backend == "sqlite":
        return ConnectionStore(settings.database_path, settings.token_encryption_key)
    if settings.storage_backend == "firestore":
        return FirestoreConnectionStore(
            settings.firestore_collection_prefix,
            settings.token_encryption_key,
            service_account_json=settings.google_service_account_json,
        )
    raise ValueError("STORAGE_BACKEND는 sqlite 또는 firestore여야 합니다.")


class ConnectionStore:
    """Encrypted user connections and minimal operational audit data.

    Questions and retrieved source text are deliberately not stored here.
    """

    def __init__(self, database_path: Path, encryption_key: str):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._fernet = Fernet(encryption_key.encode("ascii"))
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS connections (
              provider TEXT NOT NULL,
              slack_user_id TEXT NOT NULL,
              encrypted_token BLOB NOT NULL,
              expires_at INTEGER,
              scopes_json TEXT NOT NULL DEFAULT '[]',
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (provider, slack_user_id)
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
              state_digest TEXT PRIMARY KEY,
              provider TEXT NOT NULL,
              slack_user_id TEXT NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handled_events (
              event_id TEXT PRIMARY KEY,
              received_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slack_user_id TEXT NOT NULL,
              action TEXT NOT NULL,
              provider TEXT,
              occurred_at INTEGER NOT NULL,
              details_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self._connection.commit()

    def save_connection(
        self,
        *,
        provider: Provider,
        slack_user_id: str,
        access_token: str,
        expires_at: int | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        encrypted = self._fernet.encrypt(access_token.encode("utf-8"))
        self._connection.execute(
            """
            INSERT INTO connections(provider, slack_user_id, encrypted_token, expires_at, scopes_json, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, slack_user_id) DO UPDATE SET
              encrypted_token=excluded.encrypted_token,
              expires_at=excluded.expires_at,
              scopes_json=excluded.scopes_json,
              updated_at=excluded.updated_at
            """,
            (provider.value, slack_user_id, encrypted, expires_at, json.dumps(scopes or []), int(time.time())),
        )
        self.audit(slack_user_id=slack_user_id, action="connection_saved", provider=provider)
        self._connection.commit()

    def save_token_bundle(
        self,
        *,
        provider: Provider,
        slack_user_id: str,
        bundle: dict[str, object],
        scopes: list[str] | None = None,
    ) -> None:
        """Store OAuth response fields encrypted; only access_token is later exposed to readers."""
        expires_in = bundle.get("expires_in")
        expires_at = None
        if isinstance(expires_in, int):
            expires_at = int(time.time()) + expires_in - 60
        self.save_connection(
            provider=provider,
            slack_user_id=slack_user_id,
            access_token=json.dumps(bundle, separators=(",", ":")),
            expires_at=expires_at,
            scopes=scopes,
        )

    def access_token(self, *, provider: Provider, slack_user_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT encrypted_token, expires_at FROM connections WHERE provider=? AND slack_user_id=?",
            (provider.value, slack_user_id),
        ).fetchone()
        if row is None:
            return None
        # Expired OAuth access tokens may have a refresh token in the encrypted bundle.
        # The OAuth manager refreshes them before a reader is created; never discard that
        # refresh token merely because an access token has expired.
        raw = self._fernet.decrypt(row["encrypted_token"]).decode("utf-8")
        try:
            bundle = json.loads(raw)
            if isinstance(bundle, dict) and isinstance(bundle.get("access_token"), str):
                return bundle["access_token"]
        except json.JSONDecodeError:
            pass
        return raw

    def token_bundle(self, *, provider: Provider, slack_user_id: str) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT encrypted_token FROM connections WHERE provider=? AND slack_user_id=?",
            (provider.value, slack_user_id),
        ).fetchone()
        if row is None:
            return None
        raw = self._fernet.decrypt(row["encrypted_token"]).decode("utf-8")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return {"access_token": raw}
        return result if isinstance(result, dict) else None

    def delete_connection(self, *, provider: Provider, slack_user_id: str, action: str = "connection_removed") -> None:
        self._connection.execute(
            "DELETE FROM connections WHERE provider=? AND slack_user_id=?",
            (provider.value, slack_user_id),
        )
        self.audit(slack_user_id=slack_user_id, action=action, provider=provider)
        self._connection.commit()

    def connected_providers(self, slack_user_id: str) -> set[Provider]:
        rows = self._connection.execute(
            "SELECT provider FROM connections WHERE slack_user_id=?", (slack_user_id,)
        ).fetchall()
        return {Provider(row["provider"]) for row in rows}

    def save_oauth_state(self, *, digest: str, provider: Provider, slack_user_id: str, expires_at: int) -> None:
        self._connection.execute(
            "INSERT INTO oauth_states(state_digest, provider, slack_user_id, expires_at) VALUES(?, ?, ?, ?)",
            (digest, provider.value, slack_user_id, expires_at),
        )
        self._connection.commit()

    def consume_oauth_state(self, *, digest: str, provider: Provider, slack_user_id: str) -> bool:
        row = self._connection.execute(
            "SELECT expires_at FROM oauth_states WHERE state_digest=? AND provider=? AND slack_user_id=?",
            (digest, provider.value, slack_user_id),
        ).fetchone()
        self._connection.execute("DELETE FROM oauth_states WHERE state_digest=?", (digest,))
        self._connection.commit()
        return bool(row and row["expires_at"] >= int(time.time()))

    def claim_event(self, event_id: str) -> bool:
        try:
            self._connection.execute(
                "INSERT INTO handled_events(event_id, received_at) VALUES(?, ?)", (event_id, int(time.time()))
            )
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def audit(
        self,
        *,
        slack_user_id: str,
        action: str,
        provider: Provider | None = None,
        details: dict[str, str] | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO audit_events(slack_user_id, action, provider, occurred_at, details_json) VALUES(?, ?, ?, ?, ?)",
            (slack_user_id, action, provider.value if provider else None, int(time.time()), json.dumps(details or {})),
        )


class FirestoreConnectionStore:
    """Cloud Run production store. OAuth secrets remain Fernet-encrypted."""

    def __init__(self, prefix: str, encryption_key: str, *, service_account_json: str | None = None):
        from google.cloud import firestore

        if service_account_json:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_info(json.loads(service_account_json))
            self._db = firestore.Client(credentials=credentials, project=credentials.project_id)
        else:
            self._db = firestore.Client()
        self._prefix = prefix
        self._fernet = Fernet(encryption_key.encode("ascii"))

    def _doc(self, collection: str, key: str):
        return self._db.collection(f"{self._prefix}_{collection}").document(key)

    def save_connection(self, *, provider, slack_user_id, access_token, expires_at=None, scopes=None):
        encrypted = base64.urlsafe_b64encode(self._fernet.encrypt(access_token.encode())).decode()
        self._doc("connections", f"{provider.value}--{slack_user_id}").set(
            {"provider": provider.value, "slack_user_id": slack_user_id, "encrypted_token": encrypted,
             "expires_at": expires_at, "scopes": scopes or [], "updated_at": int(time.time())}
        )
        self.audit(slack_user_id=slack_user_id, action="connection_saved", provider=provider)

    def save_token_bundle(self, *, provider, slack_user_id, bundle, scopes=None):
        expires_in = bundle.get("expires_in")
        self.save_connection(provider=provider, slack_user_id=slack_user_id,
            access_token=json.dumps(bundle, separators=(",", ":")),
            expires_at=int(time.time()) + expires_in - 60 if isinstance(expires_in, int) else None,
            scopes=scopes)

    def _raw_connection(self, provider, slack_user_id):
        snapshot = self._doc("connections", f"{provider.value}--{slack_user_id}").get()
        if not snapshot.exists:
            return None
        return self._fernet.decrypt(base64.urlsafe_b64decode(snapshot.to_dict()["encrypted_token"])).decode()

    def access_token(self, *, provider, slack_user_id):
        raw = self._raw_connection(provider, slack_user_id)
        if raw is None:
            return None
        try:
            bundle = json.loads(raw)
            return bundle.get("access_token") if isinstance(bundle, dict) else raw
        except json.JSONDecodeError:
            return raw

    def token_bundle(self, *, provider, slack_user_id):
        raw = self._raw_connection(provider, slack_user_id)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"access_token": raw}
        except json.JSONDecodeError:
            return {"access_token": raw}

    def delete_connection(self, *, provider, slack_user_id, action="connection_removed"):
        self._doc("connections", f"{provider.value}--{slack_user_id}").delete()
        self.audit(slack_user_id=slack_user_id, action=action, provider=provider)

    def connected_providers(self, slack_user_id):
        docs = self._db.collection(f"{self._prefix}_connections").where("slack_user_id", "==", slack_user_id).stream()
        return {Provider(doc.to_dict()["provider"]) for doc in docs}

    def save_oauth_state(self, *, digest, provider, slack_user_id, expires_at):
        self._doc("oauth_states", digest).create({"provider": provider.value, "slack_user_id": slack_user_id, "expires_at": expires_at})

    def consume_oauth_state(self, *, digest, provider, slack_user_id):
        ref = self._doc("oauth_states", digest)
        transaction = self._db.transaction()
        from google.cloud import firestore
        @firestore.transactional
        def consume(transaction, ref):
            snapshot = ref.get(transaction=transaction)
            transaction.delete(ref)
            data = snapshot.to_dict() if snapshot.exists else None
            return bool(data and data["provider"] == provider.value and data["slack_user_id"] == slack_user_id and data["expires_at"] >= int(time.time()))
        return consume(transaction, ref)

    def claim_event(self, event_id):
        try:
            self._doc("handled_events", event_id).create({"received_at": int(time.time())})
            return True
        except Exception:
            return False

    def audit(self, *, slack_user_id, action, provider=None, details=None):
        self._db.collection(f"{self._prefix}_audit_events").add({"slack_user_id": slack_user_id, "action": action,
            "provider": provider.value if provider else None, "occurred_at": int(time.time()), "details": details or {}})
