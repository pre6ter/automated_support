from __future__ import annotations

import json
from typing import Any

from app.config import Config
from app.diagnostics import collect_chat_diagnostics, collect_diagnostics, execute_dbhub_select
from app.repository_context import extract_search_terms, search_repository_paths
from app.storage import get_message
from app.taxonomy import category_label, guess_category, normalize_category

try:
    from flask import Blueprint, current_app, jsonify, request
except ModuleNotFoundError:
    Blueprint = None
    current_app = None
    jsonify = None
    request = None


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "automated_support"
SERVER_VERSION = "0.1.0"

mcp_bp = Blueprint("mcp", __name__) if Blueprint else None


class McpProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


if mcp_bp is not None:

    @mcp_bp.post("/mcp")
    def mcp_endpoint():
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(_error_response(None, -32700, "Parse error")), 400

        response = handle_mcp_payload(payload, current_app.config["APP_CONFIG"])
        if response is None:
            return "", 202
        return jsonify(response)


def handle_mcp_payload(payload: Any, config: Config) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = [response for item in payload if (response := handle_mcp_request(item, config)) is not None]
        return responses or None
    return handle_mcp_request(payload, config)


def handle_mcp_request(payload: Any, config: Config) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return _error_response(None, -32600, "Invalid Request")

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if not isinstance(method, str):
        return _error_response(request_id, -32600, "Invalid Request")
    if not isinstance(params, dict):
        return _error_response(request_id, -32602, "Invalid params")

    try:
        result = _dispatch(method, params, config)
    except McpProtocolError as exc:
        return _error_response(request_id, exc.code, exc.message)
    except Exception as exc:
        return _error_response(request_id, -32603, str(exc))

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _dispatch(method: str, params: dict[str, Any], config: Config) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "notifications/initialized":
        return {}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tool_definitions()}
    if method == "tools/call":
        return _call_tool_result(params, config)
    raise McpProtocolError(-32601, f"Method not found: {method}")


def _call_tool_result(params: dict[str, Any], config: Config) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        raise McpProtocolError(-32602, "Tool name is required")
    if not isinstance(arguments, dict):
        raise McpProtocolError(-32602, "Tool arguments must be an object")

    try:
        result = call_project_tool(name, arguments, config)
        return {"content": [{"type": "text", "text": _json_text(result)}], "isError": False}
    except Exception as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}


def call_project_tool(name: str, arguments: dict[str, Any], config: Config) -> Any:
    if name == "classify_support_issue":
        subject = str(arguments.get("subject") or "")
        body = _required_string(arguments, "body")
        category = guess_category(subject, body)
        return {"category": category.value, "category_label": category_label(category)}

    if name == "collect_chat_diagnostics":
        question = _required_string(arguments, "question")
        history = _history_argument(arguments.get("history"))
        return collect_chat_diagnostics(config, question, history)

    if name == "collect_message_diagnostics":
        mail_id = _required_string(arguments, "mail_id")
        message = _load_message(config, mail_id)
        return {
            "message": _message_context(message),
            "diagnostics": collect_diagnostics(config, message),
        }

    if name == "get_message_context":
        mail_id = _required_string(arguments, "mail_id")
        return _message_context(_load_message(config, mail_id))

    if name == "search_repositories":
        query = _required_string(arguments, "query")
        raw_category = arguments.get("category")
        category = normalize_category(str(raw_category) if raw_category is not None else None)
        message = {"subject": query[:160], "body": query}
        terms = extract_search_terms(message, category)
        limit = _int_argument(arguments.get("limit"), config.repository_search_limit, minimum=1, maximum=20)
        return search_repository_paths(config.repository_paths, terms, limit)

    if name == "inspect_offer_number":
        offer_number = _required_string(arguments, "offer_number")
        question = f"Проверь номер предложения {offer_number}"
        return collect_chat_diagnostics(config, question, [])

    if name == "execute_dbhub_select":
        sql = _required_string(arguments, "sql")
        return {"result": execute_dbhub_select(config, sql)}

    raise ValueError(f"Unknown tool: {name}")


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "classify_support_issue",
            "description": "Определяет категорию обращения поддержки по теме и тексту.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Тема письма или короткое описание."},
                    "body": {"type": "string", "description": "Текст обращения."},
                },
                "required": ["body"],
            },
        },
        {
            "name": "collect_chat_diagnostics",
            "description": "Собирает диагностический контекст для вопроса из чата.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Вопрос пользователя."},
                    "history": {
                        "type": "array",
                        "description": "Опциональная история чата с role/content.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["question"],
            },
        },
        {
            "name": "collect_message_diagnostics",
            "description": "Собирает диагностический контекст для сохраненного письма по mail_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"mail_id": {"type": "string", "description": "ID письма в локальной базе."}},
                "required": ["mail_id"],
            },
        },
        {
            "name": "get_message_context",
            "description": "Возвращает сохраненное письмо, черновик и метаданные вложений по mail_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"mail_id": {"type": "string", "description": "ID письма в локальной базе."}},
                "required": ["mail_id"],
            },
        },
        {
            "name": "search_repositories",
            "description": "Ищет read-only совпадения в разрешенных локальных репозиториях.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Текст запроса или ошибки."},
                    "category": {
                        "type": "string",
                        "description": "Опциональная категория: converter_offers, teo_approval, other.",
                    },
                    "limit": {"type": "integer", "description": "Лимит совпадений на термин, 1-20."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "inspect_offer_number",
            "description": "Собирает контекст BuyerPro по номеру предложения, например 12177.9.",
            "inputSchema": {
                "type": "object",
                "properties": {"offer_number": {"type": "string", "description": "Номер предложения."}},
                "required": ["offer_number"],
            },
        },
        {
            "name": "execute_dbhub_select",
            "description": "Выполняет один read-only SQL-запрос к buyerpro через dbhub MCP.",
            "inputSchema": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SELECT/WITH/SHOW/EXPLAIN без мутаций."}},
                "required": ["sql"],
            },
        },
    ]


def _load_message(config: Config, mail_id: str) -> dict[str, Any]:
    message = get_message(config.database_path, mail_id)
    if not message:
        raise ValueError(f"Message not found: {mail_id}")
    return message


def _message_context(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "mail_id": message.get("mail_id"),
        "sender": message.get("sender"),
        "recipients": message.get("recipients"),
        "subject": message.get("subject"),
        "sent_at": message.get("sent_at"),
        "body": message.get("body"),
        "draft": message.get("draft"),
        "provider": message.get("provider"),
        "model": message.get("model"),
        "category": message.get("category"),
        "category_label": message.get("category_label"),
        "confidence": message.get("confidence"),
        "probable_problem": message.get("probable_problem"),
        "evidence": message.get("evidence_list", []),
        "next_checks": message.get("next_checks_list", []),
        "sources": message.get("sources_list", []),
        "attachments": [
            {
                "id": attachment.get("id"),
                "filename": attachment.get("filename"),
                "content_type": attachment.get("content_type"),
                "size": attachment.get("size"),
                "size_label": attachment.get("size_label"),
                "is_image": attachment.get("is_image"),
            }
            for attachment in message.get("attachments_list", [])
        ],
    }


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _history_argument(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("history must be an array")

    history: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and item.get("content"):
            history.append({"role": item["role"], "content": str(item["content"])})
    return history


def _int_argument(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    return max(minimum, min(maximum, parsed))


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
