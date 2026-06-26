import json
import sqlite3
from pathlib import Path
from typing import Any

from app.mail_client import IncomingEmail
from app.taxonomy import category_label


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(database_path: Path) -> None:
    with connect(database_path) as db:
        db.executescript(
            """
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
            """
        )
        _ensure_column(db, "messages", "recipients", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "suggestions", "category", "TEXT NOT NULL DEFAULT 'other'")
        _ensure_column(db, "suggestions", "confidence", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "suggestions", "probable_problem", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "suggestions", "evidence", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "suggestions", "next_checks", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "suggestions", "sources", "TEXT NOT NULL DEFAULT '[]'")


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
        return message


def list_message_attachments(database_path: Path, mail_id: str) -> list[dict[str, Any]]:
    with connect(database_path) as db:
        rows = db.execute(
            """
            SELECT filename, content_type, path, size
            FROM message_attachments
            WHERE mail_id = ?
            ORDER BY id
            """,
            (mail_id,),
        ).fetchall()
        return [dict(row) for row in rows]


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
    row["generation_in_progress"] = row.get("generation_status") in {"queued", "running"}
    return row
