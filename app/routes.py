from pathlib import Path
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from app.ai import generate_chat_answer
from app.background_jobs import start_generation_job
from app.diagnostics import collect_chat_diagnostics
from app.image_attachments import attachment_to_dict, infer_image_content_type, save_chat_uploads
from app.mail_client import default_reply_recipients, fetch_recent_emails, is_message_eligible, parse_recipients, send_reply_email
from app.storage import (
    append_chat_message,
    approve_user,
    clear_chat_conversation,
    create_chat_conversation,
    create_pending_user,
    get_message_attachment,
    get_chat_conversation,
    get_generation_job,
    get_message,
    get_user,
    get_user_by_username,
    list_chat_conversations,
    list_chat_messages,
    list_messages,
    list_users_for_approval,
    save_suggestion_send_result,
    update_suggestion_draft,
    upsert_message,
)

bp = Blueprint("main", __name__)


@bp.before_app_request
def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if not user_id:
        return

    config = current_app.config["APP_CONFIG"]
    try:
        g.user = get_user(config.database_path, int(user_id))
    except (TypeError, ValueError):
        session.pop("user_id", None)
    if not g.user:
        session.pop("user_id", None)
        return
    if not g.user.get("is_approved"):
        session.clear()
        g.user = None


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            if _wants_json():
                return jsonify({"ok": False, "error": "Требуется авторизация."}), 401
            return redirect(url_for("main.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if g.user.get("role") != "admin":
            if _wants_json():
                return jsonify({"ok": False, "error": "Для этого раздела нужны права администратора."}), 403
            flash("У вашей роли есть доступ только к чату.")
            return redirect(url_for("main.chat"))
        return view(*args, **kwargs)

    return wrapped_view


@bp.route("/login", methods=["GET", "POST"])
def login():
    config = current_app.config["APP_CONFIG"]
    if g.user:
        return redirect(_default_after_login())

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_username(config.database_path, username)
        if user and check_password_hash(str(user.get("password_hash") or ""), password):
            if not user.get("is_approved"):
                flash("Аккаунт ожидает одобрения администратора.")
                return render_template("login.html", next_url=_safe_next_url(request.form.get("next")) or "")
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            next_url = _safe_next_url(request.form.get("next"))
            if user.get("role") == "user":
                return redirect(url_for("main.chat"))
            return redirect(next_url or url_for("main.index"))

        flash("Неверный логин или пароль.")

    return render_template("login.html", next_url=_safe_next_url(request.args.get("next")) or "")


@bp.route("/register", methods=["GET", "POST"])
def register():
    config = current_app.config["APP_CONFIG"]
    if g.user:
        return redirect(_default_after_login())

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        if password != password_confirm:
            flash("Пароли не совпадают.")
        else:
            ok, message = create_pending_user(config.database_path, username, password)
            flash(message)
            if ok:
                return redirect(url_for("main.login"))

    return render_template("register.html")


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    flash("Вы вышли из системы.")
    return redirect(url_for("main.login"))


@bp.get("/")
@admin_required
def index():
    config = current_app.config["APP_CONFIG"]
    messages = [
        message
        for message in list_messages(config.database_path)
        if is_message_eligible(config, message)
    ]
    return render_template("index.html", config=config, messages=messages)


@bp.get("/admin/users")
@admin_required
def admin_users():
    config = current_app.config["APP_CONFIG"]
    return render_template("admin_users.html", users=list_users_for_approval(config.database_path))


@bp.post("/admin/users/<int:user_id>/approve")
@admin_required
def approve_registered_user(user_id: int):
    config = current_app.config["APP_CONFIG"]
    if approve_user(config.database_path, user_id):
        flash("Пользователь одобрен.")
    else:
        flash("Пользователь не найден.")
    return redirect(url_for("main.admin_users"))


@bp.get("/chat")
@login_required
def chat():
    config = current_app.config["APP_CONFIG"]
    conversations = list_chat_conversations(config.database_path, g.user["id"])
    selected_id = request.args.get("conversation_id", type=int)
    current_conversation = None
    if selected_id:
        current_conversation = get_chat_conversation(config.database_path, selected_id, g.user["id"])
    if not current_conversation and conversations:
        current_conversation = conversations[0]

    messages = []
    if current_conversation:
        messages = list_chat_messages(config.database_path, current_conversation["id"], g.user["id"])

    return render_template(
        "chat.html",
        config=config,
        messages=messages,
        conversations=conversations,
        current_conversation=current_conversation,
    )


@bp.post("/chat/new")
@login_required
def chat_new():
    config = current_app.config["APP_CONFIG"]
    conversation_id = create_chat_conversation(config.database_path, g.user["id"])
    return redirect(url_for("main.chat", conversation_id=conversation_id))


@bp.post("/chat")
@login_required
def chat_ask():
    config = current_app.config["APP_CONFIG"]
    question = request.form.get("question", "").strip()
    if not question:
        if _wants_json():
            return jsonify({"ok": False, "error": "Введите вопрос для чата."}), 400
        flash("Введите вопрос для чата.")
        return redirect(url_for("main.chat"))

    conversation_id = request.form.get("conversation_id", type=int)
    current_conversation = None
    if conversation_id:
        current_conversation = get_chat_conversation(config.database_path, conversation_id, g.user["id"])
    if not current_conversation:
        conversation_id = create_chat_conversation(config.database_path, g.user["id"], question)
    else:
        conversation_id = current_conversation["id"]

    messages = list_chat_messages(config.database_path, conversation_id, g.user["id"])
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
        saved_user_message = append_chat_message(
            config.database_path,
            conversation_id,
            g.user["id"],
            "user",
            question,
            attachments=display_images,
            sources=display_sources,
        )
        saved_assistant_message = append_chat_message(
            config.database_path,
            conversation_id,
            g.user["id"],
            "assistant",
            answer,
            provider=provider,
            model=model,
        )
        user_message = saved_user_message or user_message
        assistant_message = saved_assistant_message or assistant_message
    except Exception as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": f"Не удалось получить ответ: {exc}"}), 500
        flash(f"Не удалось получить ответ: {exc}")
        return redirect(url_for("main.chat"))

    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "conversation_id": conversation_id,
                "user": _public_chat_message(user_message),
                "assistant": _public_chat_message(assistant_message),
            }
        )
    return redirect(url_for("main.chat", conversation_id=conversation_id))


@bp.post("/chat/clear")
@login_required
def chat_clear():
    config = current_app.config["APP_CONFIG"]
    conversation_id = request.form.get("conversation_id", type=int)
    if conversation_id and clear_chat_conversation(config.database_path, conversation_id, g.user["id"]):
        flash("История диалога очищена.")
        return redirect(url_for("main.chat", conversation_id=conversation_id))

    flash("Диалог не найден.")
    return redirect(url_for("main.chat"))


def _default_after_login() -> str:
    if g.user and g.user.get("role") == "admin":
        return url_for("main.index")
    return url_for("main.chat")


def _safe_next_url(value: str | None) -> str:
    next_url = str(value or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return ""


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")


@bp.post("/sync")
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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


def _public_chat_message(message: dict[str, object]) -> dict[str, object]:
    public_message = {
        "id": message.get("id"),
        "role": message.get("role"),
        "content": message.get("content") or "",
        "attachments": message.get("attachments") or [],
    }
    if g.user and g.user.get("role") == "admin":
        public_message["sources"] = message.get("sources") or []
    return public_message
