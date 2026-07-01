import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.code_search_agent import (
    collect_agentic_code_context,
    should_run_agentic_search_for_chat,
    should_run_agentic_search_for_support,
)
from app.config import Config
from app.domain_knowledge import domain_knowledge_prompt
from app.image_attachments import image_to_ollama_payload, image_to_openai_part, is_image_attachment
from app.support_issue_parser import format_message_for_model, message_text_for_analysis
from app.taxonomy import CATEGORY_LABELS, ProblemCategory, category_label, guess_category, normalize_category


SYSTEM_PROMPT = """Ты помощник службы поддержки.
Твоя задача: определить вероятную проблему обращения и предложить вежливый, конкретный и безопасный ответ на письмо.
Используй только факты из письма и диагностического контекста. Не выдумывай номера заказов, сроки, причины сбоев или обещания.
Если в диагностике Excel есть failed-проверка source_template_reference_columns, укажи в evidence/next_checks конкретные missing_columns и mismatched_columns: букву колонки, ожидаемое название и фактическое название, если оно есть.
Если данных недостаточно, укажи это в evidence/next_checks и задай уточняющий вопрос в draft.
Не спрашивай разрешение на диагностический поиск, SQL или логи: доступные read-only проверки уже выполнены приложением до генерации ответа.
Пиши draft на языке входящего письма, если он понятен.
Не добавляй подпись, имя, должность, "С уважением", "С наилучшими пожеланиями" или похожие завершающие формулы.

Верни только JSON без markdown:
{
  "category": "converter_offers|teo_approval|other",
  "confidence": 0.0,
  "probable_problem": "краткое описание вероятной проблемы",
  "evidence": ["факт из письма или диагностики"],
  "next_checks": ["что проверить дальше, если уверенность низкая"],
  "draft": "готовый текст ответа"
}"""


CHAT_SYSTEM_PROMPT = """Ты полезный ассистент в веб-чате.
Отвечай прямо на вопрос пользователя, кратко и по делу.
Если не хватает контекста, задай уточняющий вопрос.
Не выдавай догадки за факты.
Не спрашивай разрешение выполнить поиск, SQL-запрос или проверку логов: доступные read-only диагностические действия уже выполняет приложение до ответа. Если в контексте нет результата, скажи, что данных не найдено или инструмент недоступен.
Используй диагностический контекст в таком порядке: сначала выводы из фронтенда, затем бэкенда, затем данные БД и логов.
Если в блоке dbhub_facts_first есть найденные строки, считай это фактическими данными и отвечай по ним; не пиши, что данных нет.
Если в agentic_code_search.important_results есть найденные SQL-строки, считай их фактическими данными и отвечай по ним; не пиши, что данных нет.
Если в dbhub_facts_first есть excel_file_xml_inspection, считай, что файл уже скачан и распаршен как XLSX/XML; используй найденные листы, именованные диапазоны, значения ячеек и download_teo_checks в ответе.
Если в download_teo_checks есть проверка source_template_reference_columns со статусом failed, обязательно укажи пользователю конкретные missing_columns и mismatched_columns: букву колонки, ожидаемое название и фактическое название, если оно есть.
Если пользователь просит посмотреть файл, но excel_file_xml_inspection отсутствует, не пиши, что технически не умеешь скачивать файлы. Попроси номер предложения/ТЭО или проверь, есть ли путь Converter.localFile или purch_req_request.local_file в диагностике.
Если в диагностическом контексте есть converter_problem или диагностический вывод по логам upload/normal, отвечай по нему как по главной версии; не перечисляй общие возможные причины вместо конкретной найденной причины.
Если в логах есть `Starting batch processing: 0 items`, объясни, что воркер прочитал XLSX, но не нашёл валидных строк, и попроси проверить структуру файла, наименования обязательных колонок и заполнение обязательных полей.
Если в контексте есть технические имена таблиц, колонок или файлов, используй их для понимания, но в ответе по возможности объясняй человеческими словами.
Технические названия показывай только когда без них пользователь не поймёт, о какой записи или статусе речь."""


@dataclass(frozen=True)
class SupportResponse:
    category: ProblemCategory
    confidence: float
    probable_problem: str
    evidence: list[str] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    draft: str = ""


def generate_reply(config: Config, message: dict[str, Any]) -> tuple[str, str, str]:
    analysis, provider, model = generate_support_response(config, message)
    return analysis.draft, provider, model


def generate_chat_answer(
    config: Config,
    history: list[dict[str, Any]],
    question: str,
    images: list[dict[str, Any]] | None = None,
    diagnostic_context: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    known_issue = _known_issue_chat_answer(question)
    if known_issue:
        return known_issue, "rule", "known-issue"

    provider = _normalized_provider(config.ai_provider)
    messages = _chat_messages(history, question, diagnostic_context)
    answer, model = _generate_chat_answer_once(config, provider, messages, images or [])
    if _provider_supports_agentic_search(provider) and should_run_agentic_search_for_chat(answer, diagnostic_context):
        agent_context = _collect_agentic_code_context_for_provider(config, provider, question, diagnostic_context)
        if agent_context and agent_context.get("enabled"):
            diagnostic_context = _attach_agentic_code_context(diagnostic_context, agent_context)
            messages = _chat_messages(history, question, diagnostic_context)
            answer, model = _generate_chat_answer_once(config, provider, messages, images or [])
    return _augment_chat_answer_with_excel_findings(answer, diagnostic_context), provider, model


def generate_support_response(
    config: Config,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None = None,
) -> tuple[SupportResponse, str, str]:
    known_issue = _known_issue_response(message)
    if known_issue:
        return known_issue, "rule", "known-issue"

    provider = _normalized_provider(config.ai_provider)
    images = [attachment for attachment in message.get("attachments_list") or [] if is_image_attachment(attachment)]
    response, model = _generate_support_response_once(config, provider, message, diagnostic_context, images)
    if _provider_supports_agentic_search(provider) and should_run_agentic_search_for_support(
        response,
        diagnostic_context,
        config.code_search_agent_min_confidence,
    ):
        agent_context = _collect_agentic_code_context_for_provider(
            config,
            provider,
            message_text_for_analysis(message),
            diagnostic_context,
        )
        if agent_context and agent_context.get("enabled"):
            diagnostic_context = _attach_agentic_code_context(diagnostic_context, agent_context)
            response, model = _generate_support_response_once(config, provider, message, diagnostic_context, images)
    return response, provider, model


def _generate_chat_answer_once(
    config: Config,
    provider: str,
    messages: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> tuple[str, str]:
    if provider == "openai":
        return _generate_openai_chat_answer(config, _openai_messages(messages, images)), config.openai_model
    if provider == "lmstudio":
        return _generate_lm_studio_chat_answer(config, _openai_messages(messages, images)), config.lm_studio_model
    if provider == "ollama":
        return _generate_ollama_chat_answer(config, _ollama_messages(messages, images)), config.ollama_model
    return _offline_chat_answer(str(messages[-1].get("content", ""))), "template"


def _generate_support_response_once(
    config: Config,
    provider: str,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None,
    images: list[dict[str, Any]],
) -> tuple[SupportResponse, str]:
    if provider == "openai":
        return _generate_openai_response(config, message, diagnostic_context, images), config.openai_model
    if provider == "lmstudio":
        return _generate_lm_studio_response(config, message, diagnostic_context, images), config.lm_studio_model
    if provider == "ollama":
        return _generate_ollama_response(config, message, diagnostic_context, images), config.ollama_model
    return _offline_response(message, diagnostic_context), "template"


def _provider_supports_agentic_search(provider: str) -> bool:
    return provider in {"openai", "lmstudio", "ollama"}


def _normalized_provider(provider: str) -> str:
    if provider in {"openai", "lmstudio", "ollama"}:
        return provider
    return "offline"


def _collect_agentic_code_context_for_provider(
    config: Config,
    provider: str,
    question: str,
    diagnostic_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    try:
        return collect_agentic_code_context(
            config,
            question,
            diagnostic_context,
            lambda messages: _ask_code_search_model(config, provider, messages),
        )
    except Exception:
        return None


def _ask_code_search_model(config: Config, provider: str, messages: list[dict[str, str]]) -> str:
    if provider == "openai":
        return _generate_openai_chat_answer(config, messages)
    if provider == "lmstudio":
        return _generate_lm_studio_chat_answer(config, messages)
    if provider == "ollama":
        return _generate_ollama_chat_answer(config, messages)
    raise RuntimeError(f"AI provider does not support agentic search: {provider}")


def _attach_agentic_code_context(
    diagnostic_context: dict[str, Any] | None,
    agent_context: dict[str, Any],
) -> dict[str, Any]:
    context = diagnostic_context if diagnostic_context is not None else {}
    context["agentic_code_search"] = agent_context
    sources = context.setdefault("sources", [])
    if isinstance(sources, list):
        sources.append(
            {
                "name": "agentic_code_search",
                "title": "Agentic code search",
                "summary": agent_context.get("summary", ""),
                "data": agent_context,
            }
        )
    return context


def _generate_openai_chat_answer(config: Config, messages: list[dict[str, str]]) -> str:
    import requests

    if not config.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY must be set when AI_PROVIDER=openai")

    response = requests.post(
        f"{config.openai_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.openai_model,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": config.ai_max_output_tokens,
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _clean_model_text(data["choices"][0]["message"]["content"])


def _generate_lm_studio_chat_answer(config: Config, messages: list[dict[str, str]]) -> str:
    import requests

    headers = {"Content-Type": "application/json"}
    if config.lm_studio_api_key:
        headers["Authorization"] = f"Bearer {config.lm_studio_api_key}"

    response = requests.post(
        f"{config.lm_studio_base_url}/chat/completions",
        headers=headers,
        json={
            "model": config.lm_studio_model,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": config.ai_max_output_tokens,
            "stream": False,
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _clean_model_text(data["choices"][0]["message"]["content"])


def _generate_ollama_chat_answer(config: Config, messages: list[dict[str, str]]) -> str:
    import requests

    response = requests.post(
        f"{config.ollama_base_url}/api/chat",
        json={
            "model": config.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": config.ai_max_output_tokens},
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _clean_model_text(data["message"]["content"])


def _generate_openai_response(
    config: Config,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None,
    images: list[dict[str, Any]],
) -> SupportResponse:
    import requests

    if not config.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY must be set when AI_PROVIDER=openai")

    response = requests.post(
        f"{config.openai_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.openai_model,
            "messages": _openai_messages(_messages(message, diagnostic_context), images),
            "temperature": 0.3,
            "max_tokens": config.ai_max_output_tokens,
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(_clean_model_text(data["choices"][0]["message"]["content"]), message)


def _generate_ollama_response(
    config: Config,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None,
    images: list[dict[str, Any]],
) -> SupportResponse:
    import requests

    response = requests.post(
        f"{config.ollama_base_url}/api/chat",
        json={
            "model": config.ollama_model,
            "messages": _ollama_messages(_messages(message, diagnostic_context), images),
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": config.ai_max_output_tokens},
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(_clean_model_text(data["message"]["content"]), message)


def _generate_lm_studio_response(
    config: Config,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None,
    images: list[dict[str, Any]],
) -> SupportResponse:
    import requests

    headers = {"Content-Type": "application/json"}
    if config.lm_studio_api_key:
        headers["Authorization"] = f"Bearer {config.lm_studio_api_key}"

    response = requests.post(
        f"{config.lm_studio_base_url}/chat/completions",
        headers=headers,
        json={
            "model": config.lm_studio_model,
            "messages": _openai_messages(_messages(message, diagnostic_context), images),
            "temperature": 0.3,
            "max_tokens": config.ai_max_output_tokens,
            "stream": False,
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(_clean_model_text(data["choices"][0]["message"]["content"]), message)


def _messages(message: dict[str, Any], diagnostic_context: dict[str, Any] | None) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(message, diagnostic_context)},
    ]


def _chat_messages(
    history: list[dict[str, Any]],
    question: str,
    diagnostic_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    safe_history = [
        {"role": item["role"], "content": item["content"]}
        for item in history[-20:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    system_prompt = f"{CHAT_SYSTEM_PROMPT}\n\nОсновные определения системы:\n{domain_knowledge_prompt()}"
    if diagnostic_context:
        system_prompt = (
            f"{system_prompt}\n\n"
            "Диагностический контекст из репозиториев, Grafana и dbhub buyerpro:\n"
            f"{_diagnostic_context_prompt(diagnostic_context)}"
        )
    return [{"role": "system", "content": system_prompt}, *safe_history, {"role": "user", "content": question}]


def _openai_messages(messages: list[dict[str, Any]], images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not images:
        return messages

    converted = [dict(message) for message in messages]
    for message in reversed(converted):
        if message.get("role") == "user":
            text = str(message.get("content") or "")
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            content.extend(_safe_openai_image_parts(images))
            message["content"] = content
            break
    return converted


def _ollama_messages(messages: list[dict[str, Any]], images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not images:
        return messages

    converted = [dict(message) for message in messages]
    for message in reversed(converted):
        if message.get("role") == "user":
            image_payloads = _safe_ollama_image_payloads(images)
            if image_payloads:
                message["images"] = image_payloads
            break
    return converted


def _safe_openai_image_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for image in images:
        try:
            parts.append(image_to_openai_part(image))
        except OSError:
            continue
    return parts


def _safe_ollama_image_payloads(images: list[dict[str, Any]]) -> list[str]:
    payloads: list[str] = []
    for image in images:
        try:
            payloads.append(image_to_ollama_payload(image))
        except OSError:
            continue
    return payloads


def _user_prompt(message: dict[str, Any], diagnostic_context: dict[str, Any] | None = None) -> str:
    diagnostics = _diagnostic_context_prompt(diagnostic_context)
    categories = ", ".join(f"{category.value} ({label})" for category, label in CATEGORY_LABELS.items())
    attachments = message.get("attachments_list") or []
    image_count = len([attachment for attachment in attachments if is_image_attachment(attachment)])
    attachment_note = (
        f"К письму приложено файлов: {len(attachments)}, из них изображений: {image_count}. "
        "Используй изображения как дополнительный контекст."
        if attachments
        else "К письму не приложены файлы."
    )
    body_for_model = format_message_for_model(message)
    return f"""Отправитель: {message["sender"]}
Тема: {message["subject"]}
Дата: {message["sent_at"]}

Допустимые категории:
{categories}

Основные определения системы:
{domain_knowledge_prompt()}

Содержимое письма для анализа:
{body_for_model}

Вложения:
{attachment_note}

Диагностический контекст:
{diagnostics}

Определи категорию, уверенность и вероятную проблему. Затем составь готовый черновик ответа.
Не добавляй тему письма, подпись и завершающее "С уважением", только текст ответа в поле draft."""


def _diagnostic_context_prompt(diagnostic_context: dict[str, Any] | None) -> str:
    if not diagnostic_context:
        return "Диагностический контекст не собирался."
    compact = json.dumps(_prioritized_diagnostic_context(diagnostic_context), ensure_ascii=False, indent=2)
    return compact[:18000]


def _prioritized_diagnostic_context(diagnostic_context: dict[str, Any]) -> dict[str, Any]:
    dbhub = diagnostic_context.get("dbhub") or {}
    code = diagnostic_context.get("code") or {}
    grafana = diagnostic_context.get("grafana") or {}
    repository = diagnostic_context.get("repository") or {}
    agentic_code_search = diagnostic_context.get("agentic_code_search") or {}
    buyerpro_flow_lookup = dbhub.get("buyerpro_flow_lookup", [])
    excel_file_xml_inspection = [
        item
        for item in buyerpro_flow_lookup
        if isinstance(item, dict) and item.get("query") == "excel_file_xml_inspection"
    ]
    converter_problem = next(
        (
            item
            for item in buyerpro_flow_lookup
            if isinstance(item, dict) and item.get("query") == "converter_problem_classification"
        ),
        None,
    )

    return {
        "dbhub_facts_first": {
            "agentic_code_search": _compact_agentic_code_search(agentic_code_search),
            "database": dbhub.get("database"),
            "converter_problem": converter_problem,
            "converter_upload_logs": {
                "logql": grafana.get("logql"),
                "summary": grafana.get("summary"),
                "focus": grafana.get("log_focus"),
            },
            "excel_file_xml_inspection": excel_file_xml_inspection,
            "offer_number_lookup": _non_empty_lookup_items(dbhub.get("offer_number_lookup", [])),
            "buyerpro_flow_lookup": _compact_buyerpro_flow_lookup(buyerpro_flow_lookup),
            "entity_data_lookup": _non_empty_lookup_items(dbhub.get("entity_data_lookup", [])),
        },
        "code_entity_understanding": {
            "flow": code.get("flow"),
            "user_summary": code.get("user_summary"),
            "entities": code.get("entities", []),
        },
        "repository_evidence_sample": {
            "terms": repository.get("terms", []),
            "matches": (repository.get("matches") or [])[:5],
            "errors": repository.get("errors", []),
        },
    }


def _compact_agentic_code_search(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict) or not context:
        return {}
    return {
        "enabled": context.get("enabled"),
        "summary": context.get("summary"),
        "important_results": _agentic_important_results(context),
        "steps": [_compact_agentic_step(step) for step in (context.get("steps") or [])[:10] if isinstance(step, dict)],
        "files": [
            {
                "repository": item.get("repository"),
                "path": item.get("path"),
                "line_count": item.get("line_count"),
                "truncated": item.get("truncated"),
                "content": item.get("content"),
            }
            for item in (context.get("files") or [])[:4]
            if isinstance(item, dict)
        ],
    }


def _non_empty_lookup_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and _lookup_item_has_signal(item)]


def _lookup_item_has_signal(item: dict[str, Any]) -> bool:
    if item.get("error"):
        return True
    count = item.get("count")
    if isinstance(count, int) and count > 0:
        return True
    rows = item.get("rows")
    if isinstance(rows, list) and rows:
        return True
    result = item.get("result")
    if isinstance(result, str):
        lowered = result.lower()
        if "записи не найдены" in lowered or '"rows": []' in lowered or "'rows': []" in lowered:
            return False
        return bool(result.strip())
    return False


def _agentic_important_results(context: dict[str, Any]) -> list[dict[str, Any]]:
    important: list[dict[str, Any]] = []
    for step in context.get("steps") or []:
        if not isinstance(step, dict) or step.get("action") != "execute_sql":
            continue
        result = step.get("result")
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        result_text = str(result.get("result") or "")
        if '"rows": []' in result_text or "'rows': []" in result_text:
            continue
        important.append(
            {
                "step": step.get("step"),
                "sql": step.get("sql") or result.get("sql"),
                "result": _limit_text(result_text, 2500),
                "reason": _limit_text(str(step.get("reason") or ""), 400),
            }
        )
        if len(important) >= 5:
            break
    return important


def _compact_agentic_step(step: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "step": step.get("step"),
        "action": step.get("action"),
        "query": step.get("query"),
        "sql": step.get("sql"),
        "reason": _limit_text(str(step.get("reason") or ""), 300),
    }
    result = step.get("result")
    if isinstance(result, dict):
        compact["result"] = _compact_agentic_result(result)
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def _compact_agentic_result(result: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("ok", "database", "pattern", "sql", "selector", "logql", "error"):
        if key in result:
            compact[key] = result[key]
    if "result" in result:
        compact["result"] = _limit_text(str(result.get("result") or ""), 1200)
    if isinstance(result.get("results"), list):
        compact["results"] = [
            _compact_agentic_result(item)
            for item in result["results"][:4]
            if isinstance(item, dict)
        ]
    if "matches" in result:
        compact["matches"] = result.get("matches")
    if "errors" in result:
        compact["errors"] = result.get("errors")
    return compact


def _limit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0].strip() + "\n...результат сокращён..."


def _compact_buyerpro_flow_lookup(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []

    compact: list[dict[str, Any]] = []
    problem_key = next(
        (
            item.get("problem_key")
            for item in items
            if isinstance(item, dict) and item.get("query") == "converter_problem_classification"
        ),
        None,
    )
    for item in items:
        if not isinstance(item, dict) or item.get("query") == "excel_file_xml_inspection":
            continue
        if problem_key == "converter_upload" and item.get("query") not in {
            "converter_problem_classification",
            "converter_status_for_offer_list",
        }:
            continue

        copied = dict(item)
        if copied.get("count") == 0:
            copied.pop("rows", None)
        result = copied.get("result")
        if isinstance(result, str) and len(result) > 1200:
            copied["result"] = result[:1200].rsplit("\n", 1)[0].strip() + "\n...результат сокращён..."
        compact.append(copied)
        if len(compact) >= 8:
            break
    return compact


def _augment_chat_answer_with_excel_findings(answer: str, diagnostic_context: dict[str, Any] | None) -> str:
    findings = "\n\n".join(
        item
        for item in (
            _excel_template_findings_text(diagnostic_context),
            _excel_template_quantity_findings_text(diagnostic_context),
        )
        if item
    )
    if not findings:
        return answer

    normalized_answer = answer.lower()
    if all(token.lower() in normalized_answer for token in _excel_template_finding_tokens(diagnostic_context)):
        return answer
    return f"{answer.rstrip()}\n\n{findings}"


def _excel_template_findings_text(diagnostic_context: dict[str, Any] | None) -> str:
    checks = _excel_template_reference_checks(diagnostic_context)
    lines: list[str] = []
    total_findings = 0
    for check in checks:
        details = check.get("details")
        if not isinstance(details, dict):
            continue
        for item in details.get("missing_columns") or []:
            if not isinstance(item, dict):
                continue
            column = item.get("column")
            expected = item.get("expected")
            if column and expected:
                total_findings += 1
                if len(lines) < 10:
                    lines.append(f"- пропущена/удалена колонка {column}: `{expected}`")
        for item in details.get("mismatched_columns") or []:
            if not isinstance(item, dict):
                continue
            column = item.get("column")
            expected = item.get("expected")
            actual = item.get("actual")
            if column and expected and actual:
                total_findings += 1
                if len(lines) < 10:
                    lines.append(f"- отличается название колонки {column}: ожидалось `{expected}`, в файле `{actual}`")

    if not lines:
        return ""
    if total_findings > len(lines):
        lines.append(f"- ещё расхождений: {total_findings - len(lines)}")
    return (
        "По сверке листа `Шаблон` с `templates/template_check.xlsx` найдены конкретные расхождения:\n"
        + "\n".join(lines)
    )


def _excel_template_quantity_findings_text(diagnostic_context: dict[str, Any] | None) -> str:
    lines: list[str] = []
    for check in _excel_template_item_row_checks(diagnostic_context):
        details = check.get("details")
        if not isinstance(details, dict):
            continue
        if details.get("rows_with_model", 0) <= 0 or details.get("skipped_by_empty_or_zero_quantity", 0) <= 0:
            continue
        sample = next(
            (item for item in details.get("quantity_issues") or [] if isinstance(item, dict)),
            {},
        )
        quantity_column = sample.get("quantity_column") or "AC"
        quantity_header = sample.get("quantity_header") or "F*Заказ шт"
        neighbor_column = sample.get("neighbor_quantity_column") or "AB"
        neighbor_header = sample.get("neighbor_quantity_header") or "V*Количество"
        neighbor_value = sample.get("neighbor_quantity_value")
        suffix = f"; например, в {neighbor_column} `{neighbor_header}` стоит `{neighbor_value}`" if neighbor_value else ""
        lines.append(
            f"- колонка {quantity_column} `{quantity_header}` пустая или 0 в строках с заполненной A{suffix}"
        )

    if not lines:
        return ""
    return (
        "По строкам листа `Шаблон` найдена конкретная причина, почему ТЭО не формируется:\n"
        + "\n".join(lines[:10])
    )


def _excel_template_finding_tokens(diagnostic_context: dict[str, Any] | None) -> list[str]:
    tokens: list[str] = []
    for check in _excel_template_reference_checks(diagnostic_context):
        details = check.get("details")
        if not isinstance(details, dict):
            continue
        for key in ("missing_columns", "mismatched_columns"):
            for item in details.get(key) or []:
                if not isinstance(item, dict):
                    continue
                tokens.extend(str(item.get(name) or "") for name in ("column", "expected", "actual"))
    for check in _excel_template_item_row_checks(diagnostic_context):
        details = check.get("details")
        if not isinstance(details, dict):
            continue
        for item in details.get("quantity_issues") or []:
            if not isinstance(item, dict):
                continue
            tokens.extend(
                str(item.get(name) or "")
                for name in ("quantity_column", "quantity_header", "neighbor_quantity_column", "neighbor_quantity_header")
            )
            break
    return [token for token in tokens if token]


def _excel_template_reference_checks(diagnostic_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    dbhub = (diagnostic_context or {}).get("dbhub") or {}
    checks: list[dict[str, Any]] = []
    for item in dbhub.get("buyerpro_flow_lookup") or []:
        if not isinstance(item, dict) or item.get("query") != "excel_file_xml_inspection":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        download_teo_checks = result.get("download_teo_checks")
        if not isinstance(download_teo_checks, dict):
            continue
        for check in download_teo_checks.get("checks") or []:
            if (
                isinstance(check, dict)
                and check.get("name") == "source_template_reference_columns"
                and check.get("status") == "failed"
            ):
                checks.append(check)
    return checks


def _excel_template_item_row_checks(diagnostic_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    dbhub = (diagnostic_context or {}).get("dbhub") or {}
    checks: list[dict[str, Any]] = []
    for item in dbhub.get("buyerpro_flow_lookup") or []:
        if not isinstance(item, dict) or item.get("query") != "excel_file_xml_inspection":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        download_teo_checks = result.get("download_teo_checks")
        if not isinstance(download_teo_checks, dict):
            continue
        for check in download_teo_checks.get("checks") or []:
            if (
                isinstance(check, dict)
                and check.get("name") == "source_template_item_rows"
                and check.get("status") == "failed"
            ):
                checks.append(check)
    return checks


KASPERSKY_BUYERPRO_FILE_DRAFT = (
    "Добрый день! Ошибка связана с тем, что Kaspersky блокирует загрузку файлов из BuyerPro, "
    "если размер файла больше 8 МБ.\n\n"
    "Пожалуйста, обратитесь в HelpDesk: коллеги проверят блокировку со стороны Kaspersky и помогут "
    "восстановить загрузку файлов."
)


def _known_issue_response(message: dict[str, Any]) -> SupportResponse | None:
    text = message_text_for_analysis(message)
    if not _is_kaspersky_buyerpro_file_issue(text):
        return None

    return SupportResponse(
        category=ProblemCategory.CONVERTER_OFFERS,
        confidence=0.95,
        probable_problem="Kaspersky блокирует загрузку файлов из BuyerPro больше 8 МБ.",
        evidence=["Обращение похоже на известную проблему с загрузкой файлов BuyerPro больше 8 МБ."],
        next_checks=["Пользователю нужно обратиться в HelpDesk для проверки блокировки Kaspersky."],
        draft=KASPERSKY_BUYERPRO_FILE_DRAFT,
    )


def _known_issue_chat_answer(question: str) -> str | None:
    if not _is_kaspersky_buyerpro_file_issue(question):
        return None
    return KASPERSKY_BUYERPRO_FILE_DRAFT


def _is_kaspersky_buyerpro_file_issue(text: str) -> bool:
    lowered = text.lower().replace(",", ".")
    has_kaspersky = any(term in lowered for term in ("kaspersky", "касперский", "касперского", "каспер", "kespersky"))
    has_buyerpro = any(term in lowered for term in ("buyerpro", "buyer pro", "байерпро", "байер про", "бп"))
    has_file_action = any(
        term in lowered
        for term in (
            "файл",
            "файлы",
            "выгруз",
            "загруз",
            "скач",
            "открыва",
            "не откры",
            "не выгруж",
            "не загруж",
            "не скач",
        )
    )
    has_large_file = _mentions_file_larger_than_8_mb(lowered)
    return has_file_action and ((has_large_file and has_buyerpro) or has_kaspersky)


def _mentions_file_larger_than_8_mb(text: str) -> bool:
    if any(phrase in text for phrase in ("больше 8 мб", "более 8 мб", ">8 мб", "свыше 8 мб")):
        return True

    for raw_size in re.findall(r"(\d+(?:\.\d+)?)\s*(?:мб|mb|мегабайт)", text):
        try:
            if float(raw_size) > 8:
                return True
        except ValueError:
            continue
    return False


def _offline_response(message: dict[str, Any], diagnostic_context: dict[str, Any] | None) -> SupportResponse:
    preliminary_category = (diagnostic_context or {}).get("preliminary_category")
    category = (
        normalize_category(str(preliminary_category))
        if preliminary_category
        else guess_category(message.get("subject", ""), message_text_for_analysis(message))
    )
    draft = f"""Здравствуйте!

Спасибо за ваше письмо.

Я получил(а) ваше обращение по теме «{message["subject"]}». Чтобы ответить точно, мне нужно проверить детали и при необходимости уточнить информацию.

Если вопрос срочный, пожалуйста, пришлите дополнительные подробности: номер заказа/договора, дату обращения и ожидаемый результат.
"""
    return SupportResponse(
        category=category,
        confidence=0.35 if category != ProblemCategory.OTHER else 0.1,
        probable_problem=f"Предварительная категория: {category_label(category)}",
        evidence=["Использован офлайн-шаблон без обращения к LLM."],
        next_checks=["Проверить письмо вручную или включить AI_PROVIDER для полноценной диагностики."],
        draft=_strip_sign_off(draft),
    )


def _offline_chat_answer(question: str) -> str:
    return (
        "Сейчас включён AI_PROVIDER=offline, поэтому я не могу полноценно ответить как чат-модель. "
        f"Ваш вопрос сохранён: «{question}». Включите lmstudio, openai или ollama для обычного чата."
    )


def _parse_support_response(raw_text: str, message: dict[str, Any]) -> SupportResponse:
    try:
        payload = json.loads(_extract_json(raw_text))
    except (json.JSONDecodeError, ValueError, TypeError):
        category = guess_category(message.get("subject", ""), message_text_for_analysis(message))
        return SupportResponse(
            category=category,
            confidence=0.25,
            probable_problem="Модель вернула неструктурированный ответ.",
            evidence=["Черновик сохранён из текстового ответа модели, JSON не распознан."],
            next_checks=["Проверить формат ответа модели или уменьшить температуру."],
            draft=_strip_sign_off(_clean_model_text(raw_text)),
        )

    category = normalize_category(str(payload.get("category", "")))
    return SupportResponse(
        category=category,
        confidence=_clamp_float(payload.get("confidence", 0), minimum=0, maximum=1),
        probable_problem=str(payload.get("probable_problem", "")).strip(),
        evidence=_as_str_list(payload.get("evidence")),
        next_checks=_as_str_list(payload.get("next_checks")),
        draft=_strip_sign_off(_clean_model_text(str(payload.get("draft", "")).strip())),
    )


def _clean_model_text(text: str, *, max_chars: int = 6000) -> str:
    cleaned = _truncate_repetition(str(text or "")).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars].rsplit("\n", 1)[0].strip() or cleaned[:max_chars].strip()
    return cleaned


def _truncate_repetition(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip()]
    paragraph_cut = _first_repetition_index(paragraphs, minimum_length=24)
    if paragraph_cut is not None:
        return "\n\n".join(paragraphs[:paragraph_cut]).strip()

    lines = text.strip().splitlines()
    kept: list[str] = []
    seen_counts: dict[str, int] = {}
    for line in lines:
        normalized = _normalize_repetition_unit(line)
        if normalized:
            seen_counts[normalized] = seen_counts.get(normalized, 0) + 1
            if seen_counts[normalized] >= 3:
                break
        kept.append(line)
    return "\n".join(kept).strip()


def _first_repetition_index(items: list[str], *, minimum_length: int) -> int | None:
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        normalized = _normalize_repetition_unit(item)
        if len(normalized) < minimum_length:
            continue
        seen[normalized] = seen.get(normalized, 0) + 1
        if seen[normalized] >= 2:
            return index
    return None


def _normalize_repetition_unit(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _extract_json(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON object not found")
    return stripped[start : end + 1]


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _clamp_float(value: Any, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def _strip_sign_off(text: str) -> str:
    lines = text.strip().splitlines()
    sign_offs = (
        "с уважением",
        "с наилучшими пожеланиями",
        "с благодарностью",
        "всего доброго",
        "хорошего дня",
    )

    while lines and not lines[-1].strip():
        lines.pop()

    while lines:
        normalized = lines[-1].strip().lower().rstrip(".,!")
        if normalized.startswith(sign_offs) or normalized in {"команда поддержки", "служба поддержки"}:
            lines.pop()
            continue
        break

    return "\n".join(lines).strip()
