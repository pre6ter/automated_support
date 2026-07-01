from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app.config import Config
from app.mcp_client import McpClient, load_mcp_server, mcp_text, validate_readonly_sql
from app.repository_context import search_repository_paths


AGENT_SYSTEM_PROMPT = """Ты помогаешь найти ответ по коду, БД buyerpro и логам Grafana.
Тебе доступны только read-only действия, которые выполнит приложение:
- search: поиск строки или термина по разрешённым репозиториям;
- read_file: чтение файла, найденного через search;
- list_tables: просмотр таблиц buyerpro через dbhub;
- search_schema: поиск таблиц и колонок buyerpro через dbhub;
- execute_sql: выполнение одного read-only SELECT/WITH/SHOW/EXPLAIN запроса к buyerpro;
- query_logs: поиск в Grafana Loki только по server="pro-prod2-1" и container=~"buyer.*";
- finish: завершение с кратким выводом.

Не спрашивай пользователя, можно ли выполнить доступные read-only действия. Если для ответа нужна таблица,
SQL, логи или код, сразу выбери соответствующее действие. `finish` используй только когда уже собрал
достаточно данных или инструмент вернул ошибку/пустой результат.
Для вопросов про товарные группы, сезон, разрешённые периоды, даты выдачи или ДХ сначала ищи схему
по словам `season`, `period`, `date`, `tg`, `group`, `direction`, `price`, затем выполняй SELECT
по найденным таблицам. Не делай вывод "в базе нет данных", пока не попробовал schema search и SELECT.

Верни только JSON без markdown:
{
  "action": "search|read_file|list_tables|search_schema|execute_sql|query_logs|finish",
  "query": "строка поиска, SQL-запрос или строка для поиска в логах",
  "repository": "имя репозитория, если action=read_file",
  "path": "путь файла внутри репозитория, если action=read_file",
  "reason": "почему это действие нужно",
  "summary": "вывод, если action=finish"
}

Не проси выполнить shell, запись файлов, мутации SQL, произвольную сеть или Grafana вне разрешённого selector."""

MAX_SEARCH_TERMS = 4
MAX_CONTEXT_CHARS = 16000
MAX_RESULT_TEXT = 500
MAX_QUERY_LENGTH = 4000
GRAFANA_AGENT_SELECTOR = '{server="pro-prod2-1", container=~"buyer.*"}'


@dataclass(frozen=True)
class CodeSearchAction:
    action: str
    query: str = ""
    repository: str = ""
    path: str = ""
    reason: str = ""
    summary: str = ""


def collect_agentic_code_context(
    config: Config,
    question: str,
    diagnostic_context: dict[str, Any] | None,
    ask_model: Callable[[list[dict[str, str]]], str],
) -> dict[str, Any]:
    if not config.code_search_agent_enabled:
        return {"enabled": False, "summary": "Agentic code search отключён."}
    max_steps = max(1, config.code_search_agent_max_steps)
    transcript: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    summary = ""

    for step in range(1, max_steps + 1):
        prompt = _agent_user_prompt(question, diagnostic_context, transcript, files)
        try:
            action = parse_code_search_action(ask_model([{"role": "system", "content": AGENT_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]))
        except Exception as exc:
            transcript.append({"step": step, "action": "error", "error": f"Не удалось разобрать действие модели: {exc}"})
            break

        if action.action == "search":
            result = _run_search(config, action.query)
            transcript.append(
                {
                    "step": step,
                    "action": "search",
                    "query": action.query,
                    "reason": action.reason,
                    "result": result,
                }
            )
            continue

        if action.action == "list_tables":
            result = list_buyerpro_tables(config, action.query)
            transcript.append(
                {
                    "step": step,
                    "action": "list_tables",
                    "query": action.query,
                    "reason": action.reason,
                    "result": result,
                }
            )
            continue

        if action.action == "search_schema":
            result = search_buyerpro_schema(config, action.query)
            transcript.append(
                {
                    "step": step,
                    "action": "search_schema",
                    "query": action.query,
                    "reason": action.reason,
                    "result": result,
                }
            )
            continue

        if action.action == "execute_sql":
            result = execute_buyerpro_select(config, action.query)
            transcript.append(
                {
                    "step": step,
                    "action": "execute_sql",
                    "sql": action.query,
                    "reason": action.reason,
                    "result": result,
                }
            )
            continue

        if action.action == "query_logs":
            result = query_buyer_logs(config, action.query)
            transcript.append(
                {
                    "step": step,
                    "action": "query_logs",
                    "query": action.query,
                    "reason": action.reason,
                    "result": result,
                }
            )
            continue

        if action.action == "read_file":
            result = read_repository_file(config.repository_paths, action.repository, action.path, config.code_search_agent_max_file_lines)
            transcript.append(
                {
                    "step": step,
                    "action": "read_file",
                    "repository": action.repository,
                    "path": action.path,
                    "reason": action.reason,
                    "result": _compact_file_result(result),
                }
            )
            if result.get("ok"):
                files.append(result)
            continue

        if action.action == "finish":
            summary = action.summary or action.reason
            transcript.append({"step": step, "action": "finish", "summary": summary})
            break

        transcript.append({"step": step, "action": "error", "error": f"Неизвестное действие: {action.action}"})
        break

    if not summary:
        summary = _fallback_summary(files, transcript)

    return {
        "enabled": True,
        "summary": summary,
        "steps": transcript,
        "files": files[:6],
    }


def parse_code_search_action(raw_text: str) -> CodeSearchAction:
    payload = json.loads(_extract_json(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("agent action must be a JSON object")

    action = str(payload.get("action") or "").strip().lower()
    if action not in {"search", "read_file", "list_tables", "search_schema", "execute_sql", "query_logs", "finish"}:
        raise ValueError("action must be search, read_file, list_tables, search_schema, execute_sql, query_logs or finish")

    return CodeSearchAction(
        action=action,
        query=str(payload.get("query") or "").strip()[:MAX_QUERY_LENGTH],
        repository=str(payload.get("repository") or "").strip()[:120],
        path=str(payload.get("path") or "").strip()[:500],
        reason=str(payload.get("reason") or "").strip()[:500],
        summary=str(payload.get("summary") or "").strip()[:1000],
    )


def read_repository_file(
    repository_paths: tuple[Path, ...],
    repository: str,
    relative_path: str,
    max_lines: int,
) -> dict[str, Any]:
    try:
        root = _repository_root(repository_paths, repository).expanduser().resolve()
        path = safe_repository_file_path(root, relative_path)
        if not path.is_file():
            return {"ok": False, "repository": root.name, "path": relative_path, "error": "Файл не найден."}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "repository": repository, "path": relative_path, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "repository": repository, "path": relative_path, "error": str(exc)}

    lines = text.splitlines()
    limit = max(1, max_lines)
    selected = lines[:limit]
    return {
        "ok": True,
        "repository": root.name,
        "path": str(path.relative_to(root)),
        "line_count": len(lines),
        "truncated": len(lines) > limit,
        "content": "\n".join(selected)[:6000],
    }


def safe_repository_file_path(repository_root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("Путь должен быть относительным.")
    root = repository_root.expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Путь выходит за пределы разрешённого репозитория.")
    return candidate


def list_buyerpro_tables(config: Config, query: str = "") -> dict[str, Any]:
    client = _dbhub_client(config)
    if client is None:
        return {"ok": False, "error": f"MCP server {config.mcp_dbhub_server} не найден."}

    pattern = f"%{_sql_like_term(query) or ''}%"
    try:
        result = client.call_tool(
            "search_objects_buyerpro",
            {
                "object_type": "table",
                "pattern": pattern,
                "detail_level": "summary",
                "limit": 200,
            },
        )
        return _mcp_result("buyerpro", result, pattern=pattern, limit=10000)
    except Exception as exc:
        return {"ok": False, "database": "buyerpro", "pattern": pattern, "error": str(exc)}


def search_buyerpro_schema(config: Config, query: str) -> dict[str, Any]:
    client = _dbhub_client(config)
    if client is None:
        return {"ok": False, "error": f"MCP server {config.mcp_dbhub_server} не найден."}

    pattern = f"%{_sql_like_term(query) or ''}%"
    results: list[dict[str, Any]] = []
    ok = True
    for object_type in ("table", "column"):
        try:
            result = client.call_tool(
                "search_objects_buyerpro",
                {
                    "object_type": object_type,
                    "pattern": pattern,
                    "detail_level": "summary",
                    "limit": 100,
                },
            )
            results.append({"object_type": object_type, **_mcp_result("buyerpro", result, pattern=pattern, limit=6000)})
        except Exception as exc:
            ok = False
            results.append({"object_type": object_type, "ok": False, "database": "buyerpro", "pattern": pattern, "error": str(exc)})
    return {"ok": ok and all(item.get("ok") for item in results), "database": "buyerpro", "pattern": pattern, "results": results}


def execute_buyerpro_select(config: Config, sql: str) -> dict[str, Any]:
    client = _dbhub_client(config)
    if client is None:
        return {"ok": False, "error": f"MCP server {config.mcp_dbhub_server} не найден."}

    try:
        safe_sql = validate_readonly_sql(sql)
        result = client.call_tool("execute_sql_buyerpro", {"sql": safe_sql})
        return _mcp_result("buyerpro", result, sql=safe_sql, limit=10000)
    except Exception as exc:
        return {"ok": False, "database": "buyerpro", "sql": sql[:1000], "error": str(exc)}


def query_buyer_logs(config: Config, query: str) -> dict[str, Any]:
    client = _grafana_client(config)
    if client is None:
        return {"ok": False, "error": f"MCP server {config.mcp_grafana_server} не найден."}

    try:
        datasource_uid = config.mcp_grafana_datasource_uid or _discover_loki_datasource(client)
        if not datasource_uid:
            return {"ok": False, "error": "Loki datasource не найден."}

        end = datetime.now(UTC)
        start = end - timedelta(minutes=config.mcp_log_lookback_minutes)
        logql = _agent_logql(query)
        result = client.call_tool(
            "query_loki_logs",
            {
                "datasourceUid": datasource_uid,
                "logql": logql,
                "startRfc3339": start.isoformat().replace("+00:00", "Z"),
                "endRfc3339": end.isoformat().replace("+00:00", "Z"),
                "limit": config.mcp_log_limit,
                "direction": "backward",
            },
        )
        return {
            "ok": True,
            "datasourceUid": datasource_uid,
            "selector": GRAFANA_AGENT_SELECTOR,
            "logql": logql,
            "result": mcp_text(result, limit=8000) or "Логи по запросу не найдены.",
        }
    except Exception as exc:
        return {"ok": False, "selector": GRAFANA_AGENT_SELECTOR, "error": str(exc)}


def should_run_agentic_search_for_chat(answer: str, diagnostic_context: dict[str, Any] | None) -> bool:
    _ = answer
    if _has_agentic_context(diagnostic_context):
        return False
    return not _is_hardcoded_diagnostic_case(diagnostic_context)


def should_run_agentic_search_for_support(
    response: Any,
    diagnostic_context: dict[str, Any] | None,
    min_confidence: float,
) -> bool:
    _ = response, min_confidence
    if _has_agentic_context(diagnostic_context):
        return False
    return not _is_hardcoded_diagnostic_case(diagnostic_context)


def _run_search(config: Config, query: str) -> dict[str, Any]:
    terms = [term for term in [query.strip()] if term]
    if not terms:
        return {"terms": [], "matches": [], "errors": ["Пустой поисковый запрос."]}
    context = search_repository_paths(config.repository_paths, terms[:MAX_SEARCH_TERMS], config.repository_search_limit)
    return {
        "terms": context.get("terms", []),
        "matches": (context.get("matches") or [])[:10],
        "errors": context.get("errors", []),
    }


def _dbhub_client(config: Config) -> McpClient | None:
    server = load_mcp_server(
        config.mcp_config_path,
        config.mcp_dbhub_server,
        direct_url=config.mcp_dbhub_url,
        direct_headers=config.mcp_dbhub_headers,
    )
    return McpClient(server) if server else None


def _grafana_client(config: Config) -> McpClient | None:
    server = load_mcp_server(
        config.mcp_config_path,
        config.mcp_grafana_server,
        direct_url=config.mcp_grafana_url,
        direct_headers=config.mcp_grafana_headers,
    )
    return McpClient(server) if server else None


def _discover_loki_datasource(client: McpClient) -> str:
    result = client.call_tool("list_datasources", {"type": "loki", "limit": 20})
    payload = _maybe_json_payload(result)
    candidates: Any = payload
    if isinstance(payload, dict):
        data = payload.get("data")
        candidates = data.get("datasources") if isinstance(data, dict) else payload.get("datasources")
        if candidates is None:
            candidates = payload.get("items")
    if isinstance(candidates, dict):
        candidates = candidates.values()
    for candidate in candidates or []:
        if isinstance(candidate, dict):
            uid = candidate.get("uid") or candidate.get("datasourceUid")
            if uid:
                return str(uid)
    return ""


def _agent_logql(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        return GRAFANA_AGENT_SELECTOR
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'{GRAFANA_AGENT_SELECTOR} |= "{escaped}"'


def _sql_like_term(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''").strip()


def _maybe_json_payload(result: Any) -> Any:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        texts = [item.get("text") for item in result["content"] if isinstance(item, dict) and item.get("text")]
        if texts:
            try:
                return json.loads("\n".join(str(text) for text in texts))
            except json.JSONDecodeError:
                return result
    return result


def _mcp_result(database: str, result: Any, *, limit: int, **metadata: Any) -> dict[str, Any]:
    payload = _maybe_json_payload(result)
    ok = True
    error = ""
    if isinstance(payload, dict) and payload.get("success") is False:
        ok = False
        error = str(payload.get("error") or "MCP tool returned success=false")

    output = {
        "ok": ok,
        "database": database,
        **metadata,
        "result": mcp_text(result, limit=limit),
    }
    if error:
        output["error"] = error
    return output


def _repository_root(repository_paths: tuple[Path, ...], repository: str) -> Path:
    for path in repository_paths:
        if path.name == repository:
            return path
    if len(repository_paths) == 1 and not repository:
        return repository_paths[0]
    raise ValueError(f"Репозиторий не разрешён: {repository}")


def _agent_user_prompt(
    question: str,
    diagnostic_context: dict[str, Any] | None,
    transcript: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> str:
    compact_context = json.dumps(_compact_diagnostic_context(diagnostic_context), ensure_ascii=False, indent=2)
    compact_transcript = json.dumps(transcript[-6:], ensure_ascii=False, indent=2)
    compact_files = json.dumps([_compact_file_result(item) for item in files[-4:]], ensure_ascii=False, indent=2)
    return f"""Вопрос пользователя или письмо:
{question}

Уже собранный scripted-контекст:
{compact_context[:MAX_CONTEXT_CHARS]}

Предыдущие действия agentic-поиска:
{compact_transcript[:MAX_CONTEXT_CHARS]}

Прочитанные файлы:
{compact_files[:MAX_CONTEXT_CHARS]}

Выбери следующее действие. Если достаточно контекста, верни finish."""


def _compact_diagnostic_context(diagnostic_context: dict[str, Any] | None) -> dict[str, Any]:
    if not diagnostic_context:
        return {}
    repository = diagnostic_context.get("repository") or {}
    code = diagnostic_context.get("code") or {}
    return {
        "preliminary_category": diagnostic_context.get("preliminary_category"),
        "code": {
            "user_summary": code.get("user_summary"),
            "entities": code.get("entities", []),
            "derived_terms": code.get("derived_terms", []),
        },
        "repository": {
            "terms": repository.get("terms", []),
            "matches": (repository.get("matches") or [])[:8],
            "errors": repository.get("errors", []),
        },
    }


def _compact_file_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    content = compact.get("content")
    if isinstance(content, str) and len(content) > MAX_RESULT_TEXT:
        compact["content"] = content[:MAX_RESULT_TEXT].rsplit("\n", 1)[0].strip() + "\n..."
    return compact


def _fallback_summary(files: list[dict[str, Any]], transcript: list[dict[str, Any]]) -> str:
    if files:
        names = ", ".join(f"{item.get('repository')}/{item.get('path')}" for item in files[:3])
        return f"Agentic code search прочитал файлы: {names}."
    data_actions = [str(item.get("action")) for item in transcript if item.get("action") in {"list_tables", "search_schema", "execute_sql", "query_logs"}]
    if data_actions:
        return f"Agentic diagnostics выполнил read-only действия: {', '.join(data_actions[:6])}."
    if transcript:
        return "Agentic code search выполнил поиск, но не собрал дополнительных файлов."
    return "Agentic code search не выполнил действий."


def _is_hardcoded_diagnostic_case(diagnostic_context: dict[str, Any] | None) -> bool:
    if not isinstance(diagnostic_context, dict):
        return False
    return _is_converter_upload_case(diagnostic_context) or _is_template_column_name_case(diagnostic_context)


def _is_converter_upload_case(diagnostic_context: dict[str, Any]) -> bool:
    dbhub = diagnostic_context.get("dbhub") or {}
    for item in dbhub.get("buyerpro_flow_lookup") or []:
        if isinstance(item, dict) and item.get("problem_key") == "converter_upload":
            return True

    grafana = diagnostic_context.get("grafana") or {}
    focus = grafana.get("log_focus") if isinstance(grafana, dict) else None
    return isinstance(focus, dict) and focus.get("problem") == "Проблема при загрузке в конвертер"


def _is_template_column_name_case(diagnostic_context: dict[str, Any]) -> bool:
    dbhub = diagnostic_context.get("dbhub") or {}
    for item in dbhub.get("buyerpro_flow_lookup") or []:
        if not isinstance(item, dict) or item.get("query") != "excel_file_xml_inspection":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        checks = result.get("download_teo_checks")
        if not isinstance(checks, dict):
            continue
        for check in checks.get("checks") or []:
            if (
                isinstance(check, dict)
                and check.get("name") == "source_template_reference_columns"
                and check.get("status") in {"failed", "warning"}
            ):
                return True
    return False


def _has_agentic_context(diagnostic_context: dict[str, Any] | None) -> bool:
    return bool((diagnostic_context or {}).get("agentic_code_search"))


def _extract_json(raw_text: str) -> str:
    text = raw_text.strip()
    fenced = text.strip("` \n")
    if fenced.startswith("json"):
        text = fenced[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON object not found")
    return text[start : end + 1]
