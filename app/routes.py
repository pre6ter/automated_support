from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from app.ai import generate_chat_answer
from app.background_jobs import start_generation_job
from app.diagnostics import collect_chat_diagnostics
from app.image_attachments import attachment_to_dict, infer_image_content_type, save_chat_uploads
from app.mail_client import default_reply_recipients, fetch_recent_emails, is_message_eligible, parse_recipients, send_reply_email
from app.storage import (
    get_message_attachment,
    get_generation_job,
    get_message,
    list_messages,
    save_suggestion_send_result,
    update_suggestion_draft,
    upsert_message,
)

bp = Blueprint("main", __name__)
CHAT_HISTORY_KEY = "chat_messages"


@bp.get("/")
def index():
    config = current_app.config["APP_CONFIG"]
    messages = [
        message
        for message in list_messages(config.database_path)
        if is_message_eligible(config, message)
    ]
    return render_template("index.html", config=config, messages=messages)


@bp.get("/chat")
def chat():
    config = current_app.config["APP_CONFIG"]
    return render_template("chat.html", config=config, messages=session.get(CHAT_HISTORY_KEY, []))


@bp.post("/chat")
def chat_ask():
    config = current_app.config["APP_CONFIG"]
    question = request.form.get("question", "").strip()
    if not question:
        if _wants_json():
            return jsonify({"ok": False, "error": "Введите вопрос для чата."}), 400
        flash("Введите вопрос для чата.")
        return redirect(url_for("main.chat"))

    messages = list(session.get(CHAT_HISTORY_KEY, []))
    try:
        images = [
            attachment_to_dict(attachment)
            for attachment in save_chat_uploads(
                config.attachment_dir,
                request.files.getlist("images"),
                config.max_image_attachment_bytes,
            )
        ]
        diagnostic_context = collect_chat_diagnostics(config, question, messages)
        answer, provider, model = generate_chat_answer(config, messages, question, images, diagnostic_context)
        display_images = [
            {"filename": image["filename"], "content_type": image["content_type"], "size": image["size"]}
            for image in images
        ]
        display_sources = diagnostic_context.get("sources", [])
        user_message = {"role": "user", "content": question, "attachments": display_images, "sources": display_sources}
        assistant_message = {"role": "assistant", "content": answer, "provider": provider, "model": model}
        messages.extend(
            [
                user_message,
                assistant_message,
            ]
        )
        session[CHAT_HISTORY_KEY] = messages[-40:]
    except Exception as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": f"Не удалось получить ответ: {exc}"}), 500
        flash(f"Не удалось получить ответ: {exc}")
        return redirect(url_for("main.chat"))

    if _wants_json():
        return jsonify({"ok": True, "user": user_message, "assistant": assistant_message})
    return redirect(url_for("main.chat"))


@bp.post("/chat/clear")
def chat_clear():
    session.pop(CHAT_HISTORY_KEY, None)
    flash("История чата очищена.")
    return redirect(url_for("main.chat"))


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")


@bp.post("/sync")
def sync():
    config = current_app.config["APP_CONFIG"]
    try:
        emails = fetch_recent_emails(config)
        new_count = sum(upsert_message(config.database_path, email) for email in emails)
        flash(f"Синхронизация завершена: писем получено {len(emails)}, новых {new_count}.")
    except Exception as exc:
        flash(f"Ошибка синхронизации: {exc}")
    return redirect(url_for("main.index"))


@bp.get("/messages/<mail_id>")
def show_message(mail_id: str):
    config = current_app.config["APP_CONFIG"]
    message = get_message(config.database_path, mail_id)
    if not message:
        flash("Письмо не найдено.")
        return redirect(url_for("main.index"))
    if not is_message_eligible(config, message):
        flash("Для этого письма ответ не создаётся: оно не адресовано поддержке или отправлено вами.")
        return redirect(url_for("main.index"))

    _fill_default_reply_recipients(config, message)
    return render_template("message.html", message=message)


@bp.get("/messages/<mail_id>/attachments/<int:attachment_id>")
def show_attachment(mail_id: str, attachment_id: int):
    config = current_app.config["APP_CONFIG"]
    message = get_message(config.database_path, mail_id)
    if not message or not is_message_eligible(config, message):
        abort(404)

    attachment = get_message_attachment(config.database_path, mail_id, attachment_id)
    if not attachment:
        abort(404)

    attachment_path = _safe_attachment_path(config.attachment_dir, attachment.get("path"))
    if not attachment_path or not attachment_path.is_file():
        abort(404)

    return send_file(
        attachment_path,
        mimetype=infer_image_content_type(attachment) or attachment.get("content_type") or "application/octet-stream",
        as_attachment=request.args.get("download") == "1",
        download_name=attachment.get("filename") or attachment_path.name,
    )


@bp.post("/messages/<mail_id>/reply")
def update_reply(mail_id: str):
    config = current_app.config["APP_CONFIG"]
    message = get_message(config.database_path, mail_id)
    if not message:
        flash("Письмо не найдено.")
        return redirect(url_for("main.index"))
    if not is_message_eligible(config, message):
        flash("Для этого письма ответ не создаётся: оно не адресовано поддержке или отправлено вами.")
        return redirect(url_for("main.index"))
    if not message.get("draft"):
        flash("Сначала сгенерируйте ответ.")
        return redirect(url_for("main.show_message", mail_id=mail_id))

    draft = request.form.get("draft", "").strip()
    reply_recipients = request.form.get("reply_recipients", "").strip()
    action = request.form.get("action", "save")

    if not draft:
        flash("Текст ответа не может быть пустым.")
        return redirect(url_for("main.show_message", mail_id=mail_id))
    if not reply_recipients:
        flash("Укажите получателей ответа.")
        return redirect(url_for("main.show_message", mail_id=mail_id))

    try:
        parse_recipients(reply_recipients)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("main.show_message", mail_id=mail_id))

    if not update_suggestion_draft(config.database_path, mail_id, draft, reply_recipients):
        flash("Черновик не найден. Сгенерируйте ответ заново.")
        return redirect(url_for("main.show_message", mail_id=mail_id))

    if action == "send":
        try:
            send_reply_email(config, {**message, "draft": draft}, draft, reply_recipients)
            save_suggestion_send_result(config.database_path, mail_id)
            flash("Письмо отправлено.")
        except Exception as exc:
            save_suggestion_send_result(config.database_path, mail_id, str(exc))
            flash(f"Не удалось отправить письмо: {exc}")
    else:
        flash("Правки сохранены.")

    return redirect(url_for("main.show_message", mail_id=mail_id))


@bp.get("/messages/<mail_id>/generation-status")
def generation_status(mail_id: str):
    config = current_app.config["APP_CONFIG"]
    message = get_message(config.database_path, mail_id)
    if not message:
        return jsonify({"status": "missing", "has_draft": False}), 404

    job = get_generation_job(config.database_path, mail_id) or {}
    return jsonify(
        {
            "status": job.get("status") or "idle",
            "error": job.get("error") or "",
            "has_draft": bool(message.get("draft")),
            "updated_at": job.get("updated_at"),
        }
    )


@bp.post("/messages/<mail_id>/regenerate")
def regenerate(mail_id: str):
    config = current_app.config["APP_CONFIG"]
    message = get_message(config.database_path, mail_id)
    if not message:
        flash("Письмо не найдено.")
        return redirect(url_for("main.index"))
    if not is_message_eligible(config, message):
        flash("Ответ не создан: письмо не адресовано поддержке или отправлено вами.")
        return redirect(url_for("main.index"))

    started = start_generation_job(config, message)
    if started:
        flash("Генерация ответа запущена в фоне. Страница обновится после завершения.")
    else:
        flash("Генерация уже выполняется.")

    return redirect(url_for("main.show_message", mail_id=mail_id))


def _safe_attachment_path(attachment_dir: Path, raw_path: object) -> Path | None:
    if not raw_path:
        return None

    base_dir = attachment_dir.resolve()
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError:
        return None
    return resolved


def _fill_default_reply_recipients(config: object, message: dict[str, object]) -> None:
    if message.get("reply_recipients"):
        return

    try:
        message["reply_recipients"] = default_reply_recipients(
            str(message.get("sender") or ""),
            str(message.get("recipients") or ""),
            _own_email_addresses(config),
        )
    except ValueError:
        message["reply_recipients"] = str(message.get("sender") or "").strip()


def _own_email_addresses(config: object) -> set[str]:
    return {
        str(address).strip().lower()
        for address in (
            getattr(config, "support_address", ""),
            getattr(config, "mail_username", ""),
            getattr(config, "smtp_username", ""),
            getattr(config, "owner_email", ""),
        )
        if str(address).strip()
    }
