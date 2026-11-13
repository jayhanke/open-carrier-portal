from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from open_carrier_portal.security import hash_client_secret, verify_client_secret

ADMIN_SESSION_IDLE_SECONDS = 3600


class AdminStore:
    """Local administration state, separate from partner OAuth credentials."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS local_admins (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_admin_sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_admin_failures (
                    remote TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_admin_config (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_admin_audit (
                    occurred REAL NOT NULL, username TEXT NOT NULL, operation TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_operational_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred REAL NOT NULL, level TEXT NOT NULL, component TEXT NOT NULL,
                    event TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL,
                    details TEXT NOT NULL, action_id TEXT NOT NULL DEFAULT '',
                    interaction_id TEXT NOT NULL DEFAULT ''
                );
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(local_operational_logs)")
            }
            for column in ("action_id", "interaction_id"):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE local_operational_logs ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            db.execute(
                "CREATE INDEX IF NOT EXISTS local_logs_by_interaction "
                "ON local_operational_logs(interaction_id, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS local_logs_by_action "
                "ON local_operational_logs(action_id, id)"
            )

    def set_password(self, username: str, password: str, *, reset: bool = False) -> None:
        username = username.strip().casefold()
        if not username or len(username) > 120 or not 10 <= len(password) <= 256:
            raise ValueError(
                "username required (up to 120 characters); password must be 10-256 characters"
            )
        with self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM local_admins WHERE username = ?", (username,)
            ).fetchone()
            if bool(exists) != reset:
                raise ValueError("account not found" if reset else "account already exists")
            db.execute(
                """INSERT INTO local_admins VALUES (?, ?)
                ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash""",
                (username, hash_client_secret(password)),
            )
            db.execute("DELETE FROM local_admin_sessions WHERE username = ?", (username,))
            db.execute(
                "INSERT INTO local_admin_audit VALUES (?, ?, ?)",
                (time.time(), username, "admin.password.reset" if reset else "admin.created"),
            )

    def login(self, username: str, password: str, remote: str) -> tuple[str, str] | None:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM local_admin_failures WHERE expires < ?", (now,))
            db.execute("DELETE FROM local_admin_sessions WHERE expires < ?", (now,))
            failed = db.execute(
                "SELECT count FROM local_admin_failures WHERE remote = ?", (remote,)
            ).fetchone()
            if failed and failed["count"] >= 5:
                raise ValueError("Too many login attempts. Wait five minutes.")
            user = db.execute(
                "SELECT password_hash FROM local_admins WHERE username = ?",
                (username.strip().casefold(),),
            ).fetchone()
            # The dummy hash keeps unknown-account authentication computationally expensive.
            encoded = user["password_hash"] if user else hash_client_secret("unused")
            valid = verify_client_secret(password[:256], encoded)
            if not user or not valid or len(password) > 256:
                db.execute(
                    """INSERT INTO local_admin_failures VALUES (?, 1, ?)
                    ON CONFLICT(remote) DO UPDATE SET count = count + 1""",
                    (remote, now + 300),
                )
                db.execute(
                    "INSERT INTO local_admin_audit VALUES (?, ?, ?)",
                    (now, "anonymous", "admin.login.failed"),
                )
                return None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("DELETE FROM local_admin_failures WHERE remote = ?", (remote,))
            db.execute(
                "INSERT INTO local_admin_sessions VALUES (?, ?, ?, ?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    username.strip().casefold(),
                    csrf,
                    now + ADMIN_SESSION_IDLE_SECONDS,
                ),
            )
            db.execute(
                "INSERT INTO local_admin_audit VALUES (?, ?, ?)",
                (now, username.strip().casefold(), "admin.login.success"),
            )
            return token, csrf

    def session(self, token: str) -> dict[str, str] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT username, csrf FROM local_admin_sessions "
                "WHERE token_hash = ? AND expires > ?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        with self.connection() as db:
            db.execute(
                "DELETE FROM local_admin_sessions WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def renew_session(self, token: str) -> bool:
        now = time.time()
        with self.connection() as db:
            result = db.execute(
                "UPDATE local_admin_sessions SET expires = ? WHERE token_hash = ? AND expires > ?",
                (
                    now + ADMIN_SESSION_IDLE_SECONDS,
                    hashlib.sha256(token.encode()).hexdigest(),
                    now,
                ),
            )
        return result.rowcount == 1

    def get(self, name: str) -> dict[str, str]:
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM local_admin_config WHERE name = ?", (name,)
            ).fetchone()
        return json.loads(row["value"]) if row else {}

    def save(self, name: str, value: dict[str, str]) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO local_admin_config VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value""",
                (name, json.dumps(value)),
            )

    def names(self, prefix: str) -> list[str]:
        with self.connection() as db:
            return [
                row["name"][len(prefix) :]
                for row in db.execute(
                    "SELECT name FROM local_admin_config WHERE name LIKE ? ORDER BY name",
                    (prefix + "%",),
                )
            ]

    def audit(self, username: str, operation: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO local_admin_audit VALUES (?, ?, ?)", (time.time(), username, operation)
            )

    def recent_audit(self) -> list[dict[str, str]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM local_admin_audit ORDER BY occurred DESC LIMIT 50"
            ).fetchall()
        return [{key: str(value) for key, value in dict(row).items()} for row in rows]

    def record_event(
        self,
        *,
        level: str,
        component: str,
        event: str,
        message: str,
        request_id: str,
        details: dict[str, str | int],
        action_id: str = "",
        interaction_id: str = "",
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO local_operational_logs "
                "(occurred, level, component, event, message, request_id, details, "
                "action_id, interaction_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), level, component, event, message, request_id,
                    json.dumps(details), action_id, interaction_id,
                ),
            )
            db.execute(
                "DELETE FROM local_operational_logs WHERE id NOT IN "
                "(SELECT id FROM local_operational_logs ORDER BY id DESC LIMIT 2000)"
            )

    def recent_events(
        self, component: str | None = None, *, action_id: str = "", interaction_id: str = "",
        offset: int = 0, limit: int = 100,
    ) -> list[dict[str, str]]:
        from open_carrier_portal.interactions import validate_filters

        validate_filters(action_id, interaction_id)
        if component is not None and len(component) > 64:
            raise ValueError("Invalid log component filter.")
        if not 0 <= offset <= 1900 or not 1 <= limit <= 101:
            raise ValueError("Invalid log pagination.")
        clauses = []
        parameters: list[str | int] = []
        for column, value in (
            ("component", component), ("action_id", action_id), ("interaction_id", interaction_id),
        ):
            if value is not None and value != "":
                clauses.append(f"{column} = ?")
                parameters.append(value)
        with self.connection() as db:
            rows = db.execute(
                "SELECT occurred, level, component, event, message, request_id, details, "
                "action_id, interaction_id "
                "FROM local_operational_logs "
                + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
                + "ORDER BY id DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        return [
            {
                "UTC": datetime.fromtimestamp(row["occurred"], UTC).isoformat(timespec="seconds"),
                "level": row["level"],
                "component": row["component"],
                "event": row["event"],
                "message": row["message"],
                "reference": row["request_id"],
                "details": row["details"],
                "action_id": row["action_id"],
                "interaction_id": row["interaction_id"],
            }
            for row in rows
        ]
