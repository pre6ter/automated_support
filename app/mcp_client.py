from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


READ_ONLY_SQL_PREFIXES = ("select", "with", "show", "explain")
FORBIDDEN_SQL_WORDS = {
    "alter",
    "analyze",
    "call",
    "comment",
    "copy",
    "create",
    "delete",
    "drop",
    "execute",
    "grant",
    "insert",
    "lock",
    "merge",
    "reindex",
    "replace",
    "revoke",
    "truncate",
    "update",
    "vacuum",
}


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    url: str
    headers: dict[str, str]


class McpClientError(RuntimeError):
    pass


class McpClient:
    def __init__(self, server: McpServerConfig, *, timeout: int = 45) -> None:
        import requests

        self.server = server
        self.timeout = timeout
        self._next_id = 1
        self._session = requests.Session()
        self._session_id: str | None = None
        self._initialized = False

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        self._ensure_initialized()
        return self._request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        )

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "automated_support", "version": "0.1.0"},
            },
        )
        self._notification("notifications/initialized", {})
        self._initialized = True

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = self._post(payload)
        body = _decode_mcp_response(response.text)
        if body.get("error"):
            raise McpClientError(str(body["error"]))
        return body.get("result")

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._post(payload)

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = {
            **self.server.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = self._session.post(self.server.url, headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        return response


def load_mcp_server(config_path: Path, server_name: str) -> McpServerConfig | None:
    if not config_path.exists():
        return None

    with config_path.open(encoding="utf-8") as file:
        payload = json.load(file)

    server = payload.get("mcpServers", {}).get(server_name)
    if not server or not server.get("url"):
        return None

    raw_headers = server.get("headers") or {}
    headers = {str(key): str(value) for key, value in raw_headers.items()}
    return McpServerConfig(name=server_name, url=str(server["url"]), headers=headers)


def mcp_text(result: Any, *, limit: int = 4000) -> str:
    if result is None:
        return ""
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        parts: list[str] = []
        for item in result["content"]:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("data") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()[:limit]
    return json.dumps(result, ensure_ascii=False, default=str)[:limit]


def validate_readonly_sql(sql: str) -> str:
    normalized = _strip_sql_comments(sql).strip()
    if not normalized:
        raise ValueError("SQL is empty")
    if ";" in normalized.rstrip(";"):
        raise ValueError("Only one SQL statement is allowed")

    single_statement = normalized.rstrip(";").strip()
    lowered = single_statement.lower()
    if not lowered.startswith(READ_ONLY_SQL_PREFIXES):
        raise ValueError("Only read-only SQL statements are allowed")

    tokens = set(re.findall(r"\b[a-z_]+\b", lowered))
    forbidden = tokens & FORBIDDEN_SQL_WORDS
    if forbidden:
        raise ValueError(f"Forbidden SQL keyword: {sorted(forbidden)[0]}")

    return single_statement


def _decode_mcp_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        return json.loads(stripped)

    data_lines = []
    for line in stripped.splitlines():
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        return json.loads("\n".join(data_lines))

    raise McpClientError("Unsupported MCP response format")


def _strip_sql_comments(sql: str) -> str:
    without_line_comments = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.DOTALL)

