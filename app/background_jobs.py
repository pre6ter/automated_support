from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from app.ai import generate_support_response
from app.config import Config
from app.diagnostics import collect_diagnostics
from app.storage import save_generation_job, save_suggestion


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="support-generation")
_active_jobs: set[str] = set()
_active_jobs_lock = Lock()


def start_generation_job(config: Config, message: dict[str, Any]) -> bool:
    mail_id = message["mail_id"]
    with _active_jobs_lock:
        if mail_id in _active_jobs:
            return False
        _active_jobs.add(mail_id)

    save_generation_job(config.database_path, mail_id, "queued")
    _executor.submit(_run_generation_job, config, dict(message))
    return True


def _run_generation_job(config: Config, message: dict[str, Any]) -> None:
    mail_id = message["mail_id"]
    try:
        save_generation_job(config.database_path, mail_id, "running")
        diagnostic_context = collect_diagnostics(config, message)
        analysis, provider, model = generate_support_response(config, message, diagnostic_context)
        save_suggestion(
            config.database_path,
            mail_id,
            analysis.draft,
            provider,
            model,
            category=analysis.category.value,
            confidence=analysis.confidence,
            probable_problem=analysis.probable_problem,
            evidence=analysis.evidence,
            next_checks=analysis.next_checks,
            sources=diagnostic_context.get("sources", []),
        )
        save_generation_job(config.database_path, mail_id, "succeeded")
    except Exception as exc:
        save_generation_job(config.database_path, mail_id, "failed", str(exc))
    finally:
        with _active_jobs_lock:
            _active_jobs.discard(mail_id)

