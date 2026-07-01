def create_app():
    from flask import Flask

    from app.config import load_config
    from app.mcp_server import mcp_bp
    from app.routes import bp
    from app.storage import init_db

    config = load_config()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = config.flask_secret_key
    app.config["APP_CONFIG"] = config
    app.jinja_env.filters["chat_markdown"] = _render_chat_markdown

    init_db(
        config.database_path,
        [
            {
                "username": config.auth_admin_username,
                "password": config.auth_admin_password,
                "role": "admin",
            },
            {
                "username": config.auth_user_username,
                "password": config.auth_user_password,
                "role": "user",
            },
        ],
    )
    app.register_blueprint(bp)
    app.register_blueprint(mcp_bp)
    return app


def _render_chat_markdown(value: object) -> object:
    import re

    from markupsafe import Markup, escape

    def render_inline(text: str) -> str:
        escaped = str(escape(text))
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
        return escaped

    blocks: list[str] = []
    ordered_items: list[str] = []
    unordered_items: list[str] = []

    def flush_ordered() -> None:
        nonlocal ordered_items
        if ordered_items:
            blocks.append("<ol>" + "".join(f"<li>{item}</li>" for item in ordered_items) + "</ol>")
            ordered_items = []

    def flush_unordered() -> None:
        nonlocal unordered_items
        if unordered_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in unordered_items) + "</ul>")
            unordered_items = []

    def flush_lists() -> None:
        flush_ordered()
        flush_unordered()

    for line in str(value or "").replace("\r\n", "\n").split("\n"):
        ordered_match = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        unordered_match = re.match(r"^\s*[-*]\s+(.+)$", line)

        if ordered_match:
            flush_unordered()
            ordered_items.append(render_inline(ordered_match.group(1)))
            continue
        if unordered_match:
            flush_ordered()
            unordered_items.append(render_inline(unordered_match.group(1)))
            continue

        flush_lists()
        if line.strip():
            blocks.append(f"<p>{render_inline(line)}</p>")

    flush_lists()
    return Markup("\n".join(blocks))
