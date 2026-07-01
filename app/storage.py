import json
import sqlite3
from pathlib import Path
from typing import Any

from werkzeug.security import generate_password_hash

from app.image_attachments import format_size, is_image_attachment
from app.mail_client import IncomingEmail
from app.taxonomy import category_label


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(database_path: Path, auth_users: list[dict[str, str]] | None = None) -> None:
    with connect(database_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                is_approved INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                mail_id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                recipients TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                mail_id TEXT PRIMARY KEY,
                draft TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                reply_recipients TEXT NOT NULL DEFAULT '',
                sent_at TEXT,
                sent_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mail_id) REFERENCES messages(mail_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS generation_jobs (
                mail_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mail_id) REFERENCES messages(mail_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS message_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mail_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mail_id, path),
                FOREIGN KEY (mail_id) REFERENCES messages(mail_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Новый диалог',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                attachments TEXT NOT NULL DEFAULT '[]',
                sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_column(db, "messages", "recipients", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "suggestions", "category", "TEXT NOT NULL DEFAULT 'other'")
        _ensure_column(db, "suggestions", "confidence", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "suggestions", "probable_problem", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "suggestions", "evidence", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "suggestions", "next_checks", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "suggestions", "sources", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "suggestions", "reply_recipients", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "suggestions", "sent_at", "TEXT")
        _ensure_column(db, "suggestions", "sent_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "users", "is_approved", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "approved_at", "TEXT")
        _seed_auth_users(db, auth_users or [])


def get_user(database_path: Path, user_id: int) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT id, username, password_hash, role, is_approved, approved_at, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_username(database_path: Path, username: str) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT id, username, password_hash, role, is_approved, approved_at, created_at
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()
        return dict(row) if row else None


def create_pending_user(database_path: Path, username: str, password: str) -> tuple[bool, str]:
    clean_username = username.strip()
    if not clean_username:
        return False, "Введите логин."
    if len(clean_username) > 80:
        return False, "Логин не должен быть длиннее 80 символов."
    if len(password) < 6:
        return False, "Пароль должен быть не короче 6 символов."

    with connect(database_path) as db:
        try:
            db.execute(
                """
                INSERT INTO users (username, password_hash, role, is_approved)
                VALUES (?, ?, 'user', 0)
                """,
                (clean_username, generate_password_hash(password)),
            )
        except sqlite3.IntegrityError:
            return False, "Пользователь с таким логином уже существует."
    return True, "Регистрация отправлена на одобрение администратору."


def list_users_for_approval(database_path: Path) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT id, username, role, is_approved, approved_at, created_at
            FROM users
            WHERE role = 'user'
            ORDER BY is_approved ASC, created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def approve_user(database_path: Path, user_id: int) -> bool:
    with connect(database_path) as db:
        cursor = db.execute(
            """
            UPDATE users
            SET is_approved = 1, approved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND role = 'user'
            """,
            (user_id,),
        )
        return cursor.rowcount > 0


def list_chat_conversations(database_path: Path, user_id: int) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM chat_conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_chat_conversation(database_path: Path, conversation_id: int, user_id: int) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM chat_conversations
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def create_chat_conversation(database_path: Path, user_id: int, title: str = "Новый диалог") -> int:
    clean_title = _chat_title(title)
    with connect(database_path) as db:
        cursor = db.execute(
            """
            INSERT INTO chat_conversations (user_id, title)
            VALUES (?, ?)
            """,
            (user_id, clean_title),
        )
        return int(cursor.lastrowid)


def clear_chat_conversation(database_path: Path, conversation_id: int, user_id: int) -> bool:
    with connect(database_path) as db:
        if not _chat_conversation_exists(db, conversation_id, user_id):
            return False
        db.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))
        db.execute(
            """
            UPDATE chat_conversations
            SET title = 'Новый диалог', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        return True


def list_chat_messages(database_path: Path, conversation_id: int, user_id: int) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        if not _chat_conversation_exists(db, conversation_id, user_id):
            return []
        rows = db.execute(
            """
            SELECT id, conversation_id, role, content, provider, model, attachments, sources, created_at
            FROM chat_messages
            WHERE conversation_id = ?
            ORDER BY id
            """,
            (conversation_id,),
        ).fetchall()
        return [_normalize_chat_message(dict(row)) for row in rows]


def append_chat_message(
    database_path: Path,
    conversation_id: int,
    user_id: int,
    role: str,
    content: str,
    *,
    provider: str = "",
    model: str = "",
    attachments: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    with connect(database_path) as db:
        conversation = db.execute(
            """
            SELECT id, title
            FROM chat_conversations
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        ).fetchone()
        if not conversation:
            return None

        cursor = db.execute(
            """
            INSERT INTO chat_messages (
                conversation_id, role, content, provider, model, attachments, sources
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                provider,
                model,
                _json_dumps(attachments or []),
                _json_dumps(sources or []),
            ),
        )
        if role == "user" and str(conversation["title"]) == "Новый диалог":
            db.execute(
                """
                UPDATE chat_conversations
                SET title = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (_chat_title(content), conversation_id),
            )
        else:
            db.execute(
                """
                UPDATE chat_conversations
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (conversation_id,),
            )

        row = db.execute(
            """
            SELECT id, conversation_id, role, content, provider, model, attachments, sources, created_at
            FROM chat_messages
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return _normalize_chat_message(dict(row)) if row else None


def upsert_message(database_path: Path, email: IncomingEmail) -> bool:
    with connect(database_path) as db:
        existing = db.execute("SELECT mail_id FROM messages WHERE mail_id = ?", (email.mail_id,)).fetchone()
        db.execute(
            """
            INSERT INTO messages (mail_id, sender, recipients, subject, sent_at, body)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mail_id) DO UPDATE SET
                sender = excluded.sender,
                recipients = excluded.recipients,
                subject = excluded.subject,
                sent_at = excluded.sent_at,
                body = excluded.body,
                updated_at = CURRENT_TIMESTAMP
            """,
            (email.mail_id, email.sender, email.recipients, email.subject, email.sent_at, email.body),
        )
        save_message_attachments(db, email.mail_id, getattr(email, "attachments", []))
        return existing is None


def save_message_attachments(
    db: sqlite3.Connection,
    mail_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    for attachment in attachments:
        db.execute(
            """
            INSERT OR IGNORE INTO message_attachments (mail_id, filename, content_type, path, size)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                mail_id,
                attachment["filename"],
                attachment["content_type"],
                attachment["path"],
                attachment["size"],
            ),
        )


def save_suggestion(
    database_path: Path,
    mail_id: str,
    draft: str,
    provider: str,
    model: str,
    *,
    category: str = "other",
    confidence: float = 0,
    probable_problem: str = "",
    evidence: list[str] | None = None,
    next_checks: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> None:
    with connect(database_path) as db:
        db.execute(
            """
            INSERT INTO suggestions (
                mail_id, draft, provider, model, category, confidence,
                probable_problem, evidence, next_checks, sources
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mail_id) DO UPDATE SET
                draft = excluded.draft,
                provider = excluded.provider,
                model = excluded.model,
                category = excluded.category,
                confidence = excluded.confidence,
                probable_problem = excluded.probable_problem,
                evidence = excluded.evidence,
                next_checks = excluded.next_checks,
                sources = excluded.sources,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                mail_id,
                draft,
                provider,
                model,
                category,
                confidence,
                probable_problem,
                _json_dumps(evidence or []),
                _json_dumps(next_checks or []),
                _json_dumps(sources or []),
            ),
        )


def update_suggestion_draft(database_path: Path, mail_id: str, draft: str, reply_recipients: str) -> bool:
    with connect(database_path) as db:
        cursor = db.execute(
            """
            UPDATE suggestions
            SET draft = ?, reply_recipients = ?, sent_error = ''
            WHERE mail_id = ?
            """,
            (draft, reply_recipients, mail_id),
        )
        return cursor.rowcount > 0


def save_suggestion_send_result(database_path: Path, mail_id: str, error: str = "") -> None:
    with connect(database_path) as db:
        if error:
            db.execute(
                """
                UPDATE suggestions
                SET sent_error = ?
                WHERE mail_id = ?
                """,
                (error, mail_id),
            )
            return

        db.execute(
            """
            UPDATE suggestions
            SET sent_at = CURRENT_TIMESTAMP, sent_error = ''
            WHERE mail_id = ?
            """,
            (mail_id,),
        )


def save_generation_job(database_path: Path, mail_id: str, status: str, error: str = "") -> None:
    with connect(database_path) as db:
        db.execute(
            """
            INSERT INTO generation_jobs (mail_id, status, error, started_at, finished_at, updated_at)
            VALUES (
                ?,
                ?,
                ?,
                CASE WHEN ? IN ('queued', 'running') THEN CURRENT_TIMESTAMP ELSE NULL END,
                CASE WHEN ? IN ('succeeded', 'failed') THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(mail_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                started_at = CASE
                    WHEN excluded.status IN ('queued', 'running') THEN COALESCE(generation_jobs.started_at, CURRENT_TIMESTAMP)
                    ELSE generation_jobs.started_at
                END,
                finished_at = CASE
                    WHEN excluded.status IN ('succeeded', 'failed') THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (mail_id, status, error, status, status),
        )


def get_generation_job(database_path: Path, mail_id: str) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT mail_id, status, error, started_at, finished_at, updated_at
            FROM generation_jobs
            WHERE mail_id = ?
            """,
            (mail_id,),
        ).fetchone()
        return dict(row) if row else None


def list_messages(database_path: Path) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT
                m.mail_id,
                m.sender,
                m.recipients,
                m.subject,
                m.sent_at,
                substr(m.body, 1, 350) AS preview,
                s.draft,
                s.provider,
                s.model,
                s.reply_recipients,
                s.sent_at,
                s.sent_error,
                s.category,
                s.confidence,
                s.probable_problem,
                s.created_at AS suggested_at,
                j.status AS generation_status,
                j.error AS generation_error,
                j.started_at AS generation_started_at,
                j.finished_at AS generation_finished_at,
                j.updated_at AS generation_updated_at
            FROM messages m
            LEFT JOIN suggestions s ON s.mail_id = m.mail_id
            LEFT JOIN generation_jobs j ON j.mail_id = m.mail_id
            ORDER BY m.sent_at DESC, m.created_at DESC
            """
        ).fetchall()
        return [_normalize_suggestion_row(dict(row)) for row in rows]


def get_message(database_path: Path, mail_id: str) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT
                m.mail_id,
                m.sender,
                m.recipients,
                m.subject,
                m.sent_at,
                m.body,
                s.draft,
                s.provider,
                s.model,
                s.reply_recipients,
                s.sent_at,
                s.sent_error,
                s.category,
                s.confidence,
                s.probable_problem,
                s.evidence,
                s.next_checks,
                s.sources,
                s.created_at AS suggested_at,
                j.status AS generation_status,
                j.error AS generation_error,
                j.started_at AS generation_started_at,
                j.finished_at AS generation_finished_at,
                j.updated_at AS generation_updated_at
            FROM messages m
            LEFT JOIN suggestions s ON s.mail_id = m.mail_id
            LEFT JOIN generation_jobs j ON j.mail_id = m.mail_id
            WHERE m.mail_id = ?
            """,
            (mail_id,),
        ).fetchone()
        if not row:
            return None
        message = _normalize_suggestion_row(dict(row))
        message["attachments_list"] = list_message_attachments(database_path, mail_id)
        message["image_attachments_list"] = [
            attachment for attachment in message["attachments_list"] if is_image_attachment(attachment)
        ]
        return message


def list_message_attachments(database_path: Path, mail_id: str) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT id, filename, content_type, path, size
            FROM message_attachments
            WHERE mail_id = ?
            ORDER BY id
            """,
            (mail_id,),
        ).fetchall()
        return [_normalize_attachment_row(dict(row)) for row in rows]


def get_message_attachment(database_path: Path, mail_id: str, attachment_id: int) -> dict[str, Any] | None:
    with connect(database_path) as db:
        row = db.execute(
            """
            SELECT id, filename, content_type, path, size
            FROM message_attachments
            WHERE mail_id = ? AND id = ?
            """,
            (mail_id, attachment_id),
        ).fetchone()
        return _normalize_attachment_row(dict(row)) if row else None


def messages_without_suggestions(database_path: Path) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT m.mail_id, m.sender, m.recipients, m.subject, m.sent_at, m.body
            FROM messages m
            LEFT JOIN suggestions s ON s.mail_id = m.mail_id
            WHERE s.mail_id IS NULL
            ORDER BY m.sent_at DESC, m.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _seed_auth_users(db: sqlite3.Connection, auth_users: list[dict[str, str]]) -> None:
    for user in auth_users:
        username = str(user.get("username") or "").strip()
        password = str(user.get("password") or "")
        role = str(user.get("role") or "").strip()
        if not username or not password or role not in {"admin", "user"}:
            continue

        db.execute(
            """
            INSERT INTO users (username, password_hash, role, is_approved, approved_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                role = excluded.role,
                is_approved = 1,
                approved_at = COALESCE(users.approved_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            """,
            (username, generate_password_hash(password), role),
        )


def _chat_conversation_exists(db: sqlite3.Connection, conversation_id: int, user_id: int) -> bool:
    row = db.execute(
        """
        SELECT 1
        FROM chat_conversations
        WHERE id = ? AND user_id = ?
        """,
        (conversation_id, user_id),
    ).fetchone()
    return row is not None


def _chat_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        return "Новый диалог"
    return title[:80]


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing_columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _normalize_suggestion_row(row: dict[str, Any]) -> dict[str, Any]:
    row["category_label"] = category_label(row.get("category"))
    row["evidence_list"] = _json_loads(row.get("evidence"), [])
    row["next_checks_list"] = _json_loads(row.get("next_checks"), [])
    row["sources_list"] = _json_loads(row.get("sources"), [])
    row.setdefault("attachments_list", [])
    row["image_attachments_list"] = [
        attachment for attachment in row["attachments_list"] if is_image_attachment(attachment)
    ]
    row["generation_in_progress"] = row.get("generation_status") in {"queued", "running"}
    return row


def _normalize_chat_message(row: dict[str, Any]) -> dict[str, Any]:
    row["attachments"] = _json_loads(row.get("attachments"), [])
    row["sources"] = _json_loads(row.get("sources"), [])
    return row


def _normalize_attachment_row(row: dict[str, Any]) -> dict[str, Any]:
    row["is_image"] = is_image_attachment(row)
    row["size_label"] = format_size(int(row.get("size") or 0))
    return row
