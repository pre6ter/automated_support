import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    flask_secret_key: str
    auth_admin_username: str
    auth_admin_password: str
    auth_user_username: str
    auth_user_password: str
    mail_host: str
    mail_port: int
    mail_username: str
    mail_password: str
    mail_folder: str
    mail_fetch_limit: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_ssl: bool
    support_address: str
    owner_name: str
    owner_email: str
    ai_provider: str
    ai_request_timeout_seconds: int
    ai_max_output_tokens: int
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    ollama_base_url: str
    ollama_model: str
    lm_studio_base_url: str
    lm_studio_api_key: str
    lm_studio_model: str
    database_path: Path
    attachment_dir: Path
    max_image_attachment_bytes: int
    max_email_attachment_bytes: int
    buyerpro_url: str
    excel_download_dir: Path
    max_excel_download_bytes: int
    repository_paths: tuple[Path, ...]
    repository_search_limit: int
    code_search_agent_enabled: bool
    code_search_agent_max_steps: int
    code_search_agent_max_file_lines: int
    code_search_agent_min_confidence: float
    mcp_config_path: Path
    mcp_grafana_server: str
    mcp_grafana_url: str
    mcp_grafana_headers: dict[str, str]
    mcp_dbhub_server: str
    mcp_dbhub_url: str
    mcp_dbhub_headers: dict[str, str]
    mcp_grafana_datasource_uid: str
    mcp_grafana_logql_template: str
    mcp_log_lookback_minutes: int
    mcp_log_limit: int
    diagnostics_enabled: bool


def load_config() -> Config:
    from dotenv import load_dotenv

    load_dotenv()

    mail_username = os.getenv("MAIL_USERNAME", "")
    mail_password = os.getenv("MAIL_PASSWORD", "")
    default_repositories = (
        "/Users/appleok/Documents/РАБОТА/buyerprofront",
        "/Users/appleok/Documents/РАБОТА/buyerproback",
        "/Users/appleok/Documents/РАБОТА/buyerback",
        "/Users/appleok/Documents/РАБОТА/buyerfront",
    )
    repository_paths = _split_paths(os.getenv("REPOSITORY_PATHS"), default_repositories)

    return Config(
        flask_secret_key=os.getenv("FLASK_SECRET_KEY", "dev-secret-key"),
        auth_admin_username=os.getenv("AUTH_ADMIN_USERNAME", "admin").strip(),
        auth_admin_password=os.getenv("AUTH_ADMIN_PASSWORD", "admin"),
        auth_user_username=os.getenv("AUTH_USER_USERNAME", "user").strip(),
        auth_user_password=os.getenv("AUTH_USER_PASSWORD", "user"),
        mail_host=os.getenv("MAIL_HOST", "imap.yandex.com"),
        mail_port=int(os.getenv("MAIL_PORT", "993")),
        mail_username=mail_username,
        mail_password=mail_password,
        mail_folder=os.getenv("MAIL_FOLDER", "INBOX"),
        mail_fetch_limit=int(os.getenv("MAIL_FETCH_LIMIT", "20")),
        smtp_host=os.getenv("SMTP_HOST", "smtp.yandex.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        smtp_username=os.getenv("SMTP_USERNAME") or mail_username,
        smtp_password=os.getenv("SMTP_PASSWORD") or mail_password,
        smtp_use_ssl=_env_bool("SMTP_USE_SSL", default=True),
        support_address=os.getenv("SUPPORT_ADDRESS", "buyerpro-support@famil.ru").strip().lower(),
        owner_name=os.getenv("OWNER_NAME", "Миронов Николай"),
        owner_email=os.getenv("OWNER_EMAIL", "mironov.nikolay@famil.ru").strip().lower(),
        ai_provider=os.getenv("AI_PROVIDER", "offline").strip().lower(),
        ai_request_timeout_seconds=int(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "300")),
        ai_max_output_tokens=int(os.getenv("AI_MAX_OUTPUT_TOKENS", "700")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1"),
        lm_studio_base_url=os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/"),
        lm_studio_api_key=os.getenv("LM_STUDIO_API_KEY", ""),
        lm_studio_model=os.getenv("LM_STUDIO_MODEL", "local-model"),
        database_path=Path(os.getenv("DATABASE_PATH", "data/automated_support.sqlite3")),
        attachment_dir=Path(os.getenv("ATTACHMENT_DIR", "data/attachments")),
        max_image_attachment_bytes=int(os.getenv("MAX_IMAGE_ATTACHMENT_MB", "5")) * 1024 * 1024,
        max_email_attachment_bytes=int(
            os.getenv("MAX_EMAIL_ATTACHMENT_MB", os.getenv("MAX_IMAGE_ATTACHMENT_MB", "5"))
        )
        * 1024
        * 1024,
        buyerpro_url=os.getenv("BUYERPRO_URL", "").rstrip("/"),
        excel_download_dir=Path(os.getenv("EXCEL_DOWNLOAD_DIR", "data/excel_downloads")),
        max_excel_download_bytes=int(os.getenv("MAX_EXCEL_DOWNLOAD_MB", "50")) * 1024 * 1024,
        repository_paths=repository_paths,
        repository_search_limit=int(os.getenv("REPOSITORY_SEARCH_LIMIT", "5")),
        code_search_agent_enabled=_env_bool("CODE_SEARCH_AGENT_ENABLED", default=True),
        code_search_agent_max_steps=int(os.getenv("CODE_SEARCH_AGENT_MAX_STEPS", "8")),
        code_search_agent_max_file_lines=int(os.getenv("CODE_SEARCH_AGENT_MAX_FILE_LINES", "220")),
        code_search_agent_min_confidence=float(os.getenv("CODE_SEARCH_AGENT_MIN_CONFIDENCE", "0.65")),
        mcp_config_path=Path(os.getenv("MCP_CONFIG_PATH", "~/.cursor/mcp.json")).expanduser(),
        mcp_grafana_server=os.getenv("MCP_GRAFANA_SERVER", "grafana-pro"),
        mcp_grafana_url=os.getenv("MCP_GRAFANA_URL", "").strip(),
        mcp_grafana_headers=_env_json_headers("MCP_GRAFANA_HEADERS_JSON"),
        mcp_dbhub_server=os.getenv("MCP_DBHUB_SERVER", "dbhub-prod"),
        mcp_dbhub_url=os.getenv("MCP_DBHUB_URL", "").strip(),
        mcp_dbhub_headers=_env_json_headers("MCP_DBHUB_HEADERS_JSON"),
        mcp_grafana_datasource_uid=os.getenv("MCP_GRAFANA_DATASOURCE_UID", ""),
        mcp_grafana_logql_template=os.getenv(
            "MCP_GRAFANA_LOGQL_TEMPLATE",
            '{server="pro-prod2-1", container=~"buyer.*"} |= "{query}"',
        ),
        mcp_log_lookback_minutes=int(os.getenv("MCP_LOG_LOOKBACK_MINUTES", "60")),
        mcp_log_limit=int(os.getenv("MCP_LOG_LIMIT", "20")),
        diagnostics_enabled=_env_bool("DIAGNOSTICS_ENABLED", default=True),
    )


def _split_paths(raw_value: str | None, default_paths: tuple[str, ...]) -> tuple[Path, ...]:
    values = raw_value.split(os.pathsep) if raw_value else default_paths
    return tuple(Path(value).expanduser() for value in values if value.strip())


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_json_headers(name: str) -> dict[str, str]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(key): str(value) for key, value in payload.items()}
