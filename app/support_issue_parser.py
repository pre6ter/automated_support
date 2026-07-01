from __future__ import annotations

import re
from typing import Any


FIELD_LABELS = {
    "created_at": ("дата создания",),
    "user": ("пользователь",),
    "uid": ("uid обращения", "uid"),
    "ticket_number": ("№ обращения", "номер обращения", "no обращения", "n обращения"),
    "offer_number": ("№ предложения", "номер предложения", "no предложения", "n предложения"),
    "problem_description": ("описание проблемы", "проблема", "описание"),
}

FIELD_TITLES = {
    "title": "Тип обращения",
    "created_at": "Дата создания",
    "user": "Пользователь",
    "uid": "UID обращения",
    "ticket_number": "Номер обращения",
    "offer_number": "Номер предложения",
    "problem_description": "Описание проблемы",
}


def parse_support_issue_body(body: str) -> dict[str, str]:
    """Parse structured support form fields from the plain text email body."""
    lines = _body_lines(body)
    parsed: dict[str, str] = {}
    index = 0

    while index < len(lines):
        line = lines[index]
        field, inline_value = _split_labeled_line(line)
        if not field:
            if "title" not in parsed and not _looks_like_footer(line):
                parsed["title"] = line
            index += 1
            continue

        index += 1
        values: list[str] = [inline_value] if inline_value else []
        if field == "problem_description":
            while index < len(lines):
                next_field, _ = _split_labeled_line(lines[index])
                if next_field or _looks_like_footer(lines[index]):
                    break
                values.append(lines[index])
                index += 1
        elif not values and index < len(lines):
            next_field, _ = _split_labeled_line(lines[index])
            if not next_field and not _looks_like_footer(lines[index]):
                values.append(lines[index])
                index += 1

        value = _clean_value(" ".join(values) if field != "problem_description" else "\n".join(values))
        if field != "problem_description":
            value = _trim_embedded_next_label(value)
        if value:
            parsed[field] = value

    _apply_footer_fallbacks(parsed, "\n".join(lines))
    return parsed


def format_message_for_model(message: dict[str, Any]) -> str:
    body = str(message.get("body") or "").strip()
    parsed = parse_support_issue_body(body)
    if not parsed:
        return body

    lines = ["Структурированный разбор обращения:"]
    for field in ("title", "created_at", "user", "uid", "ticket_number", "offer_number", "problem_description"):
        value = parsed.get(field)
        if value:
            lines.append(f"- {FIELD_TITLES[field]}: {value}")

    if body:
        lines.extend(["", "Исходный текст письма:", body])
    return "\n".join(lines).strip()


def message_text_for_analysis(message: dict[str, Any]) -> str:
    subject = str(message.get("subject") or "").strip()
    body = format_message_for_model(message)
    return f"{subject}\n{body}".strip()


def _body_lines(body: str) -> list[str]:
    normalized = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _split_labeled_line(line: str) -> tuple[str | None, str]:
    cleaned = re.sub(r"^[\s/|\\-]+", "", line.strip())
    if not cleaned:
        return None, ""

    label_candidate, separator, value = cleaned.partition(":")
    if not separator:
        label_candidate = cleaned
        value = ""

    normalized_label = _normalize_label(label_candidate)
    for field, labels in FIELD_LABELS.items():
        if normalized_label in labels:
            return field, value.strip()
    return None, ""


def _normalize_label(label: str) -> str:
    normalized = label.strip().lower().replace("ё", "е")
    normalized = normalized.replace("№", "№ ")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip(" .:-")
    normalized = normalized.replace("no.", "no").replace("n.", "n")
    return normalized


def _looks_like_footer(line: str) -> bool:
    return bool(re.search(r"\bBP#\d+\b", line, re.I))


def _apply_footer_fallbacks(parsed: dict[str, str], text: str) -> None:
    if "ticket_number" not in parsed:
        match = re.search(r"\bBP#\s*(\d+)\b", text, re.I)
        if match:
            parsed["ticket_number"] = match.group(1)
    if "uid" not in parsed:
        match = re.search(r"\bUID\s*:\s*([A-Za-z0-9-]+)", text, re.I)
        if match:
            parsed["uid"] = match.group(1)
    if "offer_number" not in parsed:
        match = re.search(r"(?:№|номер|no\.?|n\.?)\s*предложения\s*:?\s*([A-Za-zА-Яа-я0-9._-]+)", text, re.I)
        if match:
            parsed["offer_number"] = match.group(1)


def _clean_value(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value).strip()


def _trim_embedded_next_label(value: str) -> str:
    return re.split(r"\s+[/|\\-]+\s+(?=(?:№|номер|no\.?|n\.?)\s+\S+)", value, maxsplit=1, flags=re.I)[0].strip()
