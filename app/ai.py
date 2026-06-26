import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.domain_knowledge import domain_knowledge_prompt
from app.image_attachments import image_to_ollama_payload, image_to_openai_part
from app.taxonomy import CATEGORY_LABELS, ProblemCategory, category_label, guess_category, normalize_category


SYSTEM_PROMPT = """Ты помощник службы поддержки.
Твоя задача: определить вероятную проблему обращения и предложить вежливый, конкретный и безопасный ответ на письмо.
Используй только факты из письма и диагностического контекста. Не выдумывай номера заказов, сроки, причины сбоев или обещания.
Если данных недостаточно, укажи это в evidence/next_checks и задай уточняющий вопрос в draft.
Пиши draft на языке входящего письма, если он понятен.
Не добавляй подпись, имя, должность, "С уважением", "С наилучшими пожеланиями" или похожие завершающие формулы.

Верни только JSON без markdown:
{
  "category": "excel|sharepoint|axapta|buyerpro|integrations|other",
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
Используй диагностический контекст в таком порядке: сначала выводы из фронтенда, затем бэкенда, затем данные БД и логов.
Если в блоке dbhub_facts_first есть найденные строки, считай это фактическими данными и отвечай по ним; не пиши, что данных нет.
Если в dbhub_facts_first есть excel_file_xml_inspection, считай, что файл уже скачан и распаршен как XLSX/XML; используй найденные листы, именованные диапазоны и значения ячеек в ответе.
Если пользователь просит посмотреть файл, но excel_file_xml_inspection отсутствует, не пиши, что технически не умеешь скачивать файлы. Попроси номер предложения/ТЭО или проверь, есть ли путь Converter.localFile или purch_req_request.local_file в диагностике.
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

    provider = config.ai_provider
    messages = _chat_messages(history, question, diagnostic_context)
    if provider == "openai":
        return _generate_openai_chat_answer(config, _openai_messages(messages, images or [])), "openai", config.openai_model
    if provider == "lmstudio":
        return _generate_lm_studio_chat_answer(config, _openai_messages(messages, images or [])), "lmstudio", config.lm_studio_model
    if provider == "ollama":
        return _generate_ollama_chat_answer(config, _ollama_messages(messages, images or [])), "ollama", config.ollama_model
    return _offline_chat_answer(question), "offline", "template"


def generate_support_response(
    config: Config,
    message: dict[str, Any],
    diagnostic_context: dict[str, Any] | None = None,
) -> tuple[SupportResponse, str, str]:
    known_issue = _known_issue_response(message)
    if known_issue:
        return known_issue, "rule", "known-issue"

    provider = config.ai_provider
    images = message.get("attachments_list") or []
    if provider == "openai":
        return _generate_openai_response(config, message, diagnostic_context, images), "openai", config.openai_model
    if provider == "lmstudio":
        return _generate_lm_studio_response(config, message, diagnostic_context, images), "lmstudio", config.lm_studio_model
    if provider == "ollama":
        return _generate_ollama_response(config, message, diagnostic_context, images), "ollama", config.ollama_model
    return _offline_response(message, diagnostic_context), "offline", "template"


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
        json={"model": config.openai_model, "messages": messages, "temperature": 0.4},
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _generate_lm_studio_chat_answer(config: Config, messages: list[dict[str, str]]) -> str:
    import requests

    headers = {"Content-Type": "application/json"}
    if config.lm_studio_api_key:
        headers["Authorization"] = f"Bearer {config.lm_studio_api_key}"

    response = requests.post(
        f"{config.lm_studio_base_url}/chat/completions",
        headers=headers,
        json={"model": config.lm_studio_model, "messages": messages, "temperature": 0.4, "stream": False},
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _generate_ollama_chat_answer(config: Config, messages: list[dict[str, str]]) -> str:
    import requests

    response = requests.post(
        f"{config.ollama_base_url}/api/chat",
        json={
            "model": config.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.4},
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return data["message"]["content"].strip()


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
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(data["choices"][0]["message"]["content"], message)


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
            "options": {"temperature": 0.3},
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(data["message"]["content"], message)


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
            "stream": False,
        },
        timeout=config.ai_request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return _parse_support_response(data["choices"][0]["message"]["content"], message)


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
    attachment_note = (
        f"К письму приложено изображений: {len(attachments)}. Используй их как дополнительный контекст."
        if attachments
        else "К письму не приложены изображения."
    )
    return f"""Отправитель: {message["sender"]}
Тема: {message["subject"]}
Дата: {message["sent_at"]}

Допустимые категории:
{categories}

Основные определения системы:
{domain_knowledge_prompt()}

Текст письма:
{message["body"]}

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
    return compact[:12000]


def _prioritized_diagnostic_context(diagnostic_context: dict[str, Any]) -> dict[str, Any]:
    dbhub = diagnostic_context.get("dbhub") or {}
    code = diagnostic_context.get("code") or {}
    grafana = diagnostic_context.get("grafana") or {}
    repository = diagnostic_context.get("repository") or {}
    buyerpro_flow_lookup = dbhub.get("buyerpro_flow_lookup", [])

    return {
        "dbhub_facts_first": {
            "database": dbhub.get("database"),
            "offer_number_lookup": dbhub.get("offer_number_lookup", []),
            "buyerpro_flow_lookup": buyerpro_flow_lookup,
            "excel_file_xml_inspection": [
                item
                for item in buyerpro_flow_lookup
                if isinstance(item, dict) and item.get("query") == "excel_file_xml_inspection"
            ],
            "entity_data_lookup": dbhub.get("entity_data_lookup", []),
            "schema_search_summary": dbhub.get("summary"),
        },
        "code_entity_understanding": {
            "flow": code.get("flow"),
            "user_summary": code.get("user_summary"),
            "entities": code.get("entities", []),
            "db_terms": code.get("db_terms", []),
            "derived_terms": code.get("derived_terms", []),
        },
        "grafana_logs": {
            "logql": grafana.get("logql"),
            "summary": grafana.get("summary"),
        },
        "repository_evidence_sample": {
            "terms": repository.get("terms", []),
            "matches": (repository.get("matches") or [])[:12],
            "errors": repository.get("errors", []),
        },
        "sources": diagnostic_context.get("sources", []),
    }


KASPERSKY_BUYERPRO_FILE_DRAFT = (
    "Добрый день! Ошибка связана с тем, что Kaspersky блокирует загрузку файлов из BuyerPro, "
    "если размер файла больше 8 МБ.\n\n"
    "Пожалуйста, обратитесь в HelpDesk: коллеги проверят блокировку со стороны Kaspersky и помогут "
    "восстановить загрузку файлов."
)


def _known_issue_response(message: dict[str, Any]) -> SupportResponse | None:
    text = f"{message.get('subject', '')}\n{message.get('body', '')}"
    if not _is_kaspersky_buyerpro_file_issue(text):
        return None

    return SupportResponse(
        category=ProblemCategory.BUYERPRO,
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
        else guess_category(message.get("subject", ""), message.get("body", ""))
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
        category = guess_category(message.get("subject", ""), message.get("body", ""))
        return SupportResponse(
            category=category,
            confidence=0.25,
            probable_problem="Модель вернула неструктурированный ответ.",
            evidence=["Черновик сохранён из текстового ответа модели, JSON не распознан."],
            next_checks=["Проверить формат ответа модели или уменьшить температуру."],
            draft=_strip_sign_off(raw_text),
        )

    category = normalize_category(str(payload.get("category", "")))
    return SupportResponse(
        category=category,
        confidence=_clamp_float(payload.get("confidence", 0), minimum=0, maximum=1),
        probable_problem=str(payload.get("probable_problem", "")).strip(),
        evidence=_as_str_list(payload.get("evidence")),
        next_checks=_as_str_list(payload.get("next_checks")),
        draft=_strip_sign_off(str(payload.get("draft", "")).strip()),
    )


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
