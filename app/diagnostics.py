from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.code_intelligence import collect_code_entity_context
from app.config import Config
from app.domain_knowledge import parse_offer_numbers
from app.excel_inspector import inspect_buyerpro_excel_file
from app.mcp_client import McpClient, load_mcp_server, mcp_text, validate_readonly_sql
from app.repository_context import collect_repository_context, extract_search_terms
from app.taxonomy import ProblemCategory, category_label, guess_category


def collect_diagnostics(config: Config, message: dict[str, Any]) -> dict[str, Any]:
    preliminary_category = guess_category(message.get("subject", ""), message.get("body", ""))
    code_context = collect_code_entity_context(config, message, preliminary_category)
    repository_context = collect_repository_context(config, message, preliminary_category)
    sources: list[dict[str, Any]] = [
        _source("code", "Анализ кода: фронт -> бэк", code_context),
        _source("repository", "Локальные репозитории", repository_context),
    ]

    grafana_context = {"enabled": False, "summary": "MCP диагностика отключена."}
    dbhub_context = {"enabled": False, "summary": "MCP диагностика отключена."}

    if config.diagnostics_enabled:
        grafana_context = collect_grafana_context(config, message, preliminary_category, code_context)
        dbhub_context = collect_dbhub_context(config, message, preliminary_category, code_context)
        sources.extend(
            [
                _source("grafana", "Grafana logs", grafana_context),
                _source("dbhub", "dbhub prod", dbhub_context),
            ]
        )

    return {
        "preliminary_category": preliminary_category.value,
        "preliminary_category_label": category_label(preliminary_category),
        "code": code_context,
        "repository": repository_context,
        "grafana": grafana_context,
        "dbhub": dbhub_context,
        "sources": sources,
    }


def collect_chat_diagnostics(
    config: Config,
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    diagnostic_text = _chat_diagnostic_text(question, history or [])
    return collect_diagnostics(
        config,
        {
            "mail_id": "chat",
            "sender": "chat",
            "recipients": "",
            "subject": question[:160] or "Вопрос из чата",
            "sent_at": datetime.now(UTC).isoformat(),
            "body": diagnostic_text,
        },
    )


def _chat_diagnostic_text(question: str, history: list[dict[str, Any]]) -> str:
    recent_messages = [
        item
        for item in history[-8:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    if not recent_messages:
        return question

    lines = ["Предыдущий контекст чата:"]
    for item in recent_messages:
        role = "Пользователь" if item.get("role") == "user" else "Ассистент"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:1200]}")
    lines.append("")
    lines.append(f"Текущий вопрос пользователя: {question}")
    return "\n".join(lines)


def collect_grafana_context(
    config: Config,
    message: dict[str, Any],
    category: ProblemCategory,
    code_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    server = load_mcp_server(config.mcp_config_path, config.mcp_grafana_server)
    if not server:
        return {"enabled": False, "summary": f"MCP server {config.mcp_grafana_server} не найден."}

    client = McpClient(server)
    try:
        datasource_uid = config.mcp_grafana_datasource_uid or _discover_loki_datasource(client)
        if not datasource_uid:
            return {"enabled": True, "summary": "Loki datasource не найден."}

        term = _best_log_term(message, category, code_context)
        logql = _build_logql(config.mcp_grafana_logql_template, term)
        end = datetime.now(UTC)
        start = end - timedelta(minutes=config.mcp_log_lookback_minutes)
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
            "enabled": True,
            "datasourceUid": datasource_uid,
            "logql": logql,
            "summary": mcp_text(result, limit=3500) or "Логи по запросу не найдены.",
        }
    except Exception as exc:
        return {"enabled": True, "summary": f"Grafana MCP недоступен или запрос не выполнен: {exc}"}


def collect_dbhub_context(
    config: Config,
    message: dict[str, Any],
    category: ProblemCategory,
    code_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    server = load_mcp_server(config.mcp_config_path, config.mcp_dbhub_server)
    if not server:
        return {"enabled": False, "summary": f"MCP server {config.mcp_dbhub_server} не найден."}

    client = McpClient(server)
    terms = _diagnostic_terms(message, category, code_context)
    object_results: list[dict[str, str]] = []
    offer_number_results: list[dict[str, str]] = []
    buyerpro_flow_results: list[dict[str, Any]] = []
    entity_data_results: list[dict[str, str]] = []

    try:
        question_text = f"{message.get('subject', '')}\n{message.get('body', '')}"
        offer_number_results = _collect_offer_number_context(client, question_text)
        buyerpro_flow_results = _collect_buyerpro_flow_context(config, client, question_text)
        entity_data_results = _collect_entity_data_context(client, question_text, code_context)

        for term in terms:
            pattern = f"%{_sql_like_term(term)}%"
            for object_type in ("table", "column"):
                result = client.call_tool(
                    "search_objects_buyerpro",
                    {
                        "object_type": object_type,
                        "pattern": pattern,
                        "detail_level": "summary",
                        "limit": 20,
                    },
                )
                text = mcp_text(result, limit=1500)
                if text:
                    object_results.append({"term": term, "object_type": object_type, "result": text})

        return {
            "enabled": True,
            "database": "buyerpro",
            "terms": terms,
            "offer_number_lookup": offer_number_results,
            "buyerpro_flow_lookup": buyerpro_flow_results,
            "entity_data_lookup": entity_data_results,
            "summary": object_results or "Подходящие объекты БД по ключевым словам не найдены.",
        }
    except Exception as exc:
        return {"enabled": True, "summary": f"dbhub MCP недоступен или запрос не выполнен: {exc}"}


def execute_dbhub_select(config: Config, sql: str) -> str:
    safe_sql = validate_readonly_sql(sql)
    server = load_mcp_server(config.mcp_config_path, config.mcp_dbhub_server)
    if not server:
        raise RuntimeError(f"MCP server {config.mcp_dbhub_server} не найден")
    client = McpClient(server)
    return mcp_text(client.call_tool("execute_sql_buyerpro", {"sql": safe_sql}), limit=6000)


def _collect_offer_number_context(client: McpClient, text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for brand_id, number, raw_value in parse_offer_numbers(text)[:3]:
        for label, sql in _offer_number_queries(brand_id, number):
            try:
                result = client.call_tool("execute_sql_buyerpro", {"sql": validate_readonly_sql(sql)})
                result_text = mcp_text(result, limit=2500)
            except Exception as exc:
                result_text = f"Не удалось выполнить read-only запрос: {exc}"
            results.append({"offer_number": raw_value, "query": label, "result": result_text or "Записи не найдены."})
    return results


def _collect_buyerpro_flow_context(config: Config, client: McpClient, text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    file_refs: list[dict[str, str]] = []
    for brand_id, number, raw_value in parse_offer_numbers(text)[:3]:
        for label, sql in _buyerpro_flow_queries(brand_id, number):
            try:
                result = client.call_tool("execute_sql_buyerpro", {"sql": validate_readonly_sql(sql)})
                result_text = mcp_text(result, limit=3000)
                file_refs.extend(_extract_excel_file_refs(label, result))
            except Exception as exc:
                result_text = f"Не удалось выполнить read-only запрос: {exc}"
            results.append({"offer_number": raw_value, "query": label, "result": result_text or "Записи не найдены."})
    results.extend(_inspect_excel_file_refs(config, file_refs))
    return results


def _collect_entity_data_context(
    client: McpClient,
    text: str,
    code_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not code_context:
        return []

    identifiers = _extract_lookup_identifiers(text)
    if not identifiers["numbers"] and not identifiers["tokens"]:
        return []

    results: list[dict[str, str]] = []
    for entity in code_context.get("entities", [])[:3]:
        entity_key = str(entity.get("key") or "")
        for label, sql in _entity_lookup_queries(entity_key, identifiers):
            try:
                result = client.call_tool("execute_sql_buyerpro", {"sql": validate_readonly_sql(sql)})
                result_text = mcp_text(result, limit=2500)
            except Exception as exc:
                result_text = f"Не удалось выполнить read-only запрос: {exc}"
            results.append({"entity": entity_key, "query": label, "result": result_text or "Записи не найдены."})
    return results


def _extract_lookup_identifiers(text: str) -> dict[str, list[str]]:
    numbers = re.findall(r"\b\d{2,}\b", text)
    tokens = re.findall(r"\b[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9_.-]{2,}\b", text)
    stop_words = {
        "какой",
        "какая",
        "какое",
        "статус",
        "номер",
        "предложения",
        "предложение",
        "заявка",
        "заказ",
        "что",
        "сейчас",
    }
    cleaned_tokens = [
        token
        for token in tokens
        if token.lower() not in stop_words and not re.fullmatch(r"\d+", token)
    ]
    return {"numbers": _unique_values(numbers)[:6], "tokens": _unique_values(cleaned_tokens)[:8]}


def _entity_lookup_queries(entity_key: str, identifiers: dict[str, list[str]]) -> list[tuple[str, str]]:
    number_values = ", ".join(identifiers["numbers"]) or "null"
    text_conditions = _text_conditions(identifiers["tokens"])

    if entity_key == "purchase_request":
        conditions = []
        if identifiers["numbers"]:
            conditions.extend([f"pr.id in ({number_values})", f"pr.converter_id in ({number_values})"])
        if text_conditions:
            conditions.append(
                " or ".join(
                    [
                        f"pr.purch_req_num ilike any (array[{text_conditions}])",
                        f"pr.link ilike any (array[{text_conditions}])",
                        f"pr.status ilike any (array[{text_conditions}])",
                    ]
                )
            )
        return [
            (
                "purch_req_request",
                f"""
                select
                    pr.id,
                    pr.converter_id,
                    pr.status,
                    pr.purch_req_num,
                    pr.link,
                    pr.offer_type,
                    pr.approved_date,
                    pr."createdAt",
                    pr."updatedAt"
                from public.purch_req_request pr
                where {_or_conditions(conditions)}
                order by pr."updatedAt" desc
                limit 10
                """,
            )
        ]

    if entity_key == "production_order":
        conditions = []
        if identifiers["numbers"]:
            conditions.extend([f"po.id in ({number_values})", f"po.converter_id in ({number_values})"])
        if text_conditions:
            conditions.append(
                " or ".join(
                    [
                        f"po.converter_number ilike any (array[{text_conditions}])",
                        f"po.purch_req_number ilike any (array[{text_conditions}])",
                        f"po.axapta_order_id ilike any (array[{text_conditions}])",
                        f"po.purchase_number ilike any (array[{text_conditions}])",
                        f"po.status ilike any (array[{text_conditions}])",
                        f"po.request_status ilike any (array[{text_conditions}])",
                    ]
                )
            )
        return [
            (
                "production_order",
                f"""
                select
                    po.id,
                    po.converter_id,
                    po.converter_number,
                    po.purch_req_number,
                    po.status,
                    po.request_status,
                    po.request_error,
                    po.axapta_order_id,
                    po.purchase_number,
                    po.created_at,
                    po.updated_at
                from public.production_order po
                where {_or_conditions(conditions)}
                order by po.updated_at desc
                limit 10
                """,
            )
        ]

    if entity_key == "offer":
        return []

    return []


def _text_conditions(tokens: list[str]) -> str:
    values = [f"'%{_sql_literal_like(token)}%'" for token in tokens]
    return ", ".join(values)


def _or_conditions(conditions: list[str]) -> str:
    if not conditions:
        return "false"
    return "(" + ") or (".join(conditions) + ")"


def _unique_values(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _offer_number_queries(brand_id: int, number: int) -> list[tuple[str, str]]:
    return [
        (
            "Converter",
            f"""
            select
                c.id,
                c."brandId",
                c.number,
                c.status::text as status,
                c.teostatus::text as teostatus,
                c.teoerror,
                c.ax_status,
                c."teoNum",
                c."exportId",
                c."localFile",
                c.disabled,
                c."userId",
                c."authorId",
                c."brandTitle",
                c.provider,
                c."createdAt",
                c."updatedAt"
            from public."Converter" c
            where c."brandId" = {brand_id} and c.number = {number}
            limit 5
            """,
        ),
        (
            "purch_req_request",
            f"""
            select
                pr.id,
                pr.converter_id,
                pr.status,
                pr.purch_req_num,
                pr.link,
                pr.offer_type,
                pr.approved_date,
                pr."createdAt",
                pr."updatedAt"
            from public.purch_req_request pr
            join public."Converter" c on c.id = pr.converter_id
            where c."brandId" = {brand_id} and c.number = {number}
            order by pr."updatedAt" desc
            limit 10
            """,
        ),
        (
            "production_order",
            f"""
            select
                po.id,
                po.converter_id,
                po.converter_number,
                po.purch_req_number,
                po.status,
                po.request_status,
                po.request_error,
                po.axapta_order_id,
                po.created_at,
                po.updated_at
            from public.production_order po
            join public."Converter" c on c.id = po.converter_id
            where c."brandId" = {brand_id} and c.number = {number}
            order by po.updated_at desc
            limit 10
            """,
        ),
    ]


def _buyerpro_flow_queries(brand_id: int, number: int) -> list[tuple[str, str]]:
    converter_cte = f"""
        with converter_match as (
            select c.*
            from public."Converter" c
            where c."brandId" = {brand_id} and c.number = {number}
            order by c."updatedAt" desc
            limit 5
        )
    """
    teo_cte = f"""
        with converter_match as (
            select c.*
            from public."Converter" c
            where c."brandId" = {brand_id} and c.number = {number}
            order by c."updatedAt" desc
            limit 5
        ),
        teo_match as (
            select pr.*
            from public.purch_req_request pr
            join converter_match c on pr.converter_id = c.id or pr.id = c."exportId"
        )
    """
    return [
        (
            "converter_status_for_offer_list",
            f"""
            {converter_cte}
            select
                c.id,
                concat(c."brandId", '.', c.number) as offer_number,
                c.status::text as status,
                c.teostatus::text as teostatus,
                c.teoerror,
                c.ax_status,
                c."teoNum",
                c."exportId",
                c."localFile",
                c.disabled,
                c."userId",
                c."authorId",
                c."brandTitle",
                c.provider,
                c."createdAt",
                c."updatedAt"
            from converter_match c
            order by c."updatedAt" desc
            """,
        ),
        (
            "teo_request_for_approval",
            f"""
            {teo_cte}
            select
                pr.id,
                pr.converter_id,
                pr.status::text as status,
                pr.purch_req_num,
                pr.user_id,
                pr.author_id,
                pr.new_user_applicant_id,
                pr.new_user_approver_id,
                pr.buyer_sum,
                pr.local_file,
                pr.approved_date,
                pr."createdAt",
                pr."updatedAt"
            from teo_match pr
            order by pr."updatedAt" desc
            limit 10
            """,
        ),
        (
            "teo_direction_approvals",
            f"""
            {teo_cte}
            select
                a.id,
                a.acsapta_id,
                a.direction,
                a.summ,
                a.status::text as status,
                a.user_id,
                a.new_user_approver_id,
                a.approval_file_id
            from public.acsapta_teo_approve a
            join teo_match pr on pr.id = a.acsapta_id
            order by a.id
            limit 20
            """,
        ),
        (
            "teo_recent_activity",
            f"""
            {teo_cte}
            select
                c.id,
                c."acsaptaId",
                c."userId",
                c."createdAt"
            from public."AcsaptaTeoComment" c
            join teo_match pr on pr.id = c."acsaptaId"
            order by c."createdAt" desc
            limit 10
            """,
        ),
        (
            "teo_user_identity",
            f"""
            {teo_cte}
            select
                pr.id as teo_id,
                pr.buyer_sum,
                responsible.id as responsible_user_id,
                responsible.email as responsible_email,
                responsible.fio as responsible_fio,
                responsible."displayName" as responsible_display_name,
                responsible.position as responsible_position,
                responsible.num as responsible_num,
                responsible.to as responsible_mail_to,
                author.id as author_user_id,
                author.email as author_email,
                author.fio as author_fio,
                author."displayName" as author_display_name,
                author.position as author_position,
                author.num as author_num,
                applicant_sync.id as applicant_sync_id,
                applicant_sync.email as applicant_email,
                applicant_sync.full_name as applicant_full_name,
                applicant_sync.position as applicant_position,
                applicant_sync.num as applicant_num,
                applicant_sync.is_fired as applicant_is_fired,
                approver_sync.id as approver_sync_id,
                approver_sync.email as approver_email,
                approver_sync.full_name as approver_full_name,
                approver_sync.position as approver_position,
                approver_sync.num as approver_num,
                approver_sync.is_fired as approver_is_fired
            from teo_match pr
            left join public."User" responsible on responsible.id = pr.user_id
            left join public."User" author on author.id = pr.author_id
            left join public."user" applicant_sync on applicant_sync.id = pr.new_user_applicant_id
            left join public."user" approver_sync on approver_sync.id = pr.new_user_approver_id
            order by pr."updatedAt" desc
            limit 10
            """,
        ),
        (
            "teo_user_directions",
            f"""
            {teo_cte}
            select distinct
                pr.id as teo_id,
                role_map.role,
                sync_user.id as sync_user_id,
                sync_user.email,
                sync_user.full_name,
                sync_user.position,
                direction.id as direction_id,
                direction.key as direction_key,
                direction.title as direction_title,
                user_direction.is_from_api
            from teo_match pr
            cross join lateral (
                values
                    ('applicant', pr.new_user_applicant_id),
                    ('approver', pr.new_user_approver_id)
            ) as role_map(role, sync_user_id)
            join public."user" sync_user on sync_user.id = role_map.sync_user_id
            left join public.user_direction user_direction on user_direction.user_id = sync_user.id
            left join public."ExDirection" direction on direction.id = user_direction.direction_id
            order by pr.id, role_map.role, direction.title
            limit 80
            """,
        ),
        (
            "teo_permission_rules_for_amount",
            f"""
            {teo_cte}
            select
                pr.id as teo_id,
                pr.buyer_sum,
                rule.id as rule_id,
                rule.type,
                rule.position,
                rule.user_id,
                rule.approve_amount_from,
                rule.approve_amount_to,
                rule.mail_amount_from,
                rule.mail_amount_to,
                sync_user.email,
                sync_user.full_name,
                sync_user.position as user_position,
                sync_user.is_fired
            from teo_match pr
            join public.permission_rule rule
              on (
                coalesce(rule.approve_amount_from, 0) <= coalesce(pr.buyer_sum, 0)
                and coalesce(rule.approve_amount_to, 0) >= coalesce(pr.buyer_sum, 0)
              )
              or (
                coalesce(rule.mail_amount_from, 0) <= coalesce(pr.buyer_sum, 0)
                and coalesce(rule.mail_amount_to, 0) >= coalesce(pr.buyer_sum, 0)
              )
            left join public."user" sync_user on sync_user.id = rule.user_id
            order by rule.type, rule.position, sync_user.full_name, rule.id
            limit 80
            """,
        ),
        (
            "exdirection_reference",
            f"""
            select
                direction.id,
                direction.key,
                direction.title,
                direction.tgs
            from public."ExDirection" direction
            order by direction.title
            limit 200
            """,
        ),
    ]


def _extract_excel_file_refs(query_label: str, result: Any) -> list[dict[str, str]]:
    payload = _maybe_json_payload(result)
    if not isinstance(payload, dict):
        return []

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_rows = data.get("rows") if isinstance(data, dict) else payload.get("rows")
    rows = [row for row in raw_rows or [] if isinstance(row, dict)]

    refs: list[dict[str, str]] = []
    for row in rows:
        if query_label == "converter_status_for_offer_list":
            storage_path = str(row.get("localFile") or row.get("local_file") or "").strip()
            source = "Converter.localFile"
        elif query_label == "teo_request_for_approval":
            storage_path = str(row.get("local_file") or row.get("localFile") or "").strip()
            source = "purch_req_request.local_file"
        else:
            continue

        if storage_path:
            refs.append(
                {
                    "source": source,
                    "query": query_label,
                    "record_id": str(row.get("id") or ""),
                    "storage_path": storage_path,
                }
            )
    return refs


def _inspect_excel_file_refs(config: Config, file_refs: list[dict[str, str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for file_ref in file_refs:
        storage_path = file_ref["storage_path"]
        if storage_path in seen_paths:
            continue
        seen_paths.add(storage_path)

        try:
            inspection = inspect_buyerpro_excel_file(
                buyerpro_url=config.buyerpro_url,
                storage_path=storage_path,
                download_dir=config.excel_download_dir,
                max_bytes=config.max_excel_download_bytes,
            )
        except Exception as exc:
            inspection = {
                "enabled": bool(config.buyerpro_url),
                "summary": f"Не удалось скачать или распарсить Excel-файл: {exc}",
            }

        results.append(
            {
                "query": "excel_file_xml_inspection",
                "source": file_ref["source"],
                "record_id": file_ref["record_id"],
                "storage_path": storage_path,
                "result": inspection,
            }
        )
        if len(results) >= 4:
            break
    return results


def _discover_loki_datasource(client: McpClient) -> str:
    result = client.call_tool("list_datasources", {"type": "loki", "limit": 20})
    result = _maybe_json_payload(result)
    if isinstance(result, dict):
        candidates = result.get("datasources") or result.get("items") or result.get("data") or []
    elif isinstance(result, list):
        candidates = result
    else:
        candidates = []

    for candidate in candidates:
        if isinstance(candidate, dict):
            uid = candidate.get("uid") or candidate.get("datasourceUid")
            if uid:
                return str(uid)
    return ""


def _maybe_json_payload(result: Any) -> Any:
    if isinstance(result, dict) and "content" in result:
        text = mcp_text(result, limit=20000)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return result
    return result


def _best_log_term(message: dict[str, Any], category: ProblemCategory, code_context: dict[str, Any] | None = None) -> str:
    terms = _diagnostic_terms(message, category, code_context)
    if terms:
        return terms[0]
    return category_label(category)


def _diagnostic_terms(
    message: dict[str, Any],
    category: ProblemCategory,
    code_context: dict[str, Any] | None,
) -> list[str]:
    terms = extract_search_terms(message, category)
    if code_context:
        terms.extend(str(term) for term in code_context.get("db_terms", []))
        terms.extend(str(term) for term in code_context.get("derived_terms", []))

    unique_terms: list[str] = []
    for term in terms:
        cleaned = str(term).strip()
        if not cleaned or cleaned.lower() in {item.lower() for item in unique_terms}:
            continue
        unique_terms.append(cleaned)
    return unique_terms[:8]


def _build_logql(template: str, term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return template.replace("{query}", escaped)


def _sql_like_term(term: str) -> str:
    return term.replace("%", "\\%").replace("_", "\\_")[:80]


def _sql_literal_like(term: str) -> str:
    return _sql_like_term(term).replace("'", "''")


def _source(kind: str, title: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "summary": _compact_summary(payload),
    }


def _compact_summary(payload: dict[str, Any]) -> str:
    user_summary = payload.get("user_summary")
    if isinstance(user_summary, str) and user_summary:
        return user_summary[:500]
    buyerpro_flow_lookup = payload.get("buyerpro_flow_lookup")
    if isinstance(buyerpro_flow_lookup, list) and buyerpro_flow_lookup:
        return f"Выполнен lookup flow Список предложений -> ТЭО: {len(buyerpro_flow_lookup)} read-only результатов."
    entity_data_lookup = payload.get("entity_data_lookup")
    if isinstance(entity_data_lookup, list) and entity_data_lookup:
        return f"Выполнен lookup по сущностям: {len(entity_data_lookup)} read-only результатов."
    offer_number_lookup = payload.get("offer_number_lookup")
    if isinstance(offer_number_lookup, list) and offer_number_lookup:
        return f"Выполнен lookup номера предложения: {len(offer_number_lookup)} read-only результатов."
    summary = payload.get("summary")
    if isinstance(summary, str):
        return summary[:500]
    if isinstance(summary, list):
        return f"Найдено записей: {len(summary)}"
    matches = payload.get("matches")
    if isinstance(matches, list):
        return f"Найдено совпадений: {len(matches)}"
    return "Контекст собран."

