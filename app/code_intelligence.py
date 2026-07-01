from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.repository_context import extract_search_terms, search_repository_paths
from app.support_issue_parser import message_text_for_analysis
from app.taxonomy import ProblemCategory


FRONTEND_REPOSITORIES = {"buyerprofront", "buyerfront"}
BACKEND_REPOSITORIES = {"buyerproback", "buyerback"}


@dataclass(frozen=True)
class EntityRule:
    key: str
    user_label: str
    db_terms: tuple[str, ...]
    code_terms: tuple[str, ...]


ENTITY_RULES = (
    EntityRule(
        key="offer",
        user_label="предложение",
        db_terms=("Converter", "brandId", "number", "converter_id", "exportId", "teostatus", "teoerror", "purch_req_request", "production_order"),
        code_terms=(
            "номер предложения",
            "список предложений",
            "предложение",
            "converter",
            "upload/normal",
            "exportteo",
            "createteo",
            "converter_id",
            "brandId",
            "production_order",
        ),
    ),
    EntityRule(
        key="teo_approval",
        user_label="согласование ТЭО",
        db_terms=(
            "purch_req_request",
            "acsapta_teo_approve",
            "AcsaptaTeoComment",
            "new_user_approver_id",
            "approved_date",
            "permission_rule",
            "user_direction",
            "ExDirection",
            "User",
            "user",
        ),
        code_terms=(
            "согласование ТЭО",
            "acsaptateo",
            "approve",
            "userstatus",
            "revoke",
            "на доработке",
            "ожидает согласования",
            "permission_rule",
            "user_direction",
            "ExDirection",
        ),
    ),
    EntityRule(
        key="purchase_request",
        user_label="заявка на закупку",
        db_terms=("purch_req_request", "purch_req_num", "approved_date", "converter_id"),
        code_terms=("purch_req", "заявка", "закуп", "approved_date"),
    ),
    EntityRule(
        key="production_order",
        user_label="заказ на производство",
        db_terms=("production_order", "request_status", "request_error", "axapta_order_id", "converter_id"),
        code_terms=("production_order", "заказ", "производств", "request_status", "axapta_order_id"),
    ),
    EntityRule(
        key="sku",
        user_label="товарная позиция",
        db_terms=("sku", "item_id", "converter_id"),
        code_terms=("sku", "товар", "номенклатур", "item_id"),
    ),
)


def collect_code_entity_context(
    config: Config,
    message: dict[str, Any],
    category: ProblemCategory,
) -> dict[str, Any]:
    initial_terms = extract_search_terms(message, category)
    frontend_paths, backend_paths = _split_repository_paths(config.repository_paths)

    frontend_context = search_repository_paths(frontend_paths, initial_terms, config.repository_search_limit)
    derived_terms = _derive_terms_from_matches(message, frontend_context.get("matches", []))
    backend_terms = _unique_terms([*initial_terms, *derived_terms])
    backend_context = search_repository_paths(backend_paths, backend_terms, config.repository_search_limit)

    entities = _infer_entities(message, frontend_context.get("matches", []), backend_context.get("matches", []))
    db_terms = _unique_terms([term for entity in entities for term in entity["db_terms"]])

    return {
        "flow": "frontend_code -> backend_code -> db/logs",
        "frontend": frontend_context,
        "backend": backend_context,
        "derived_terms": derived_terms,
        "entities": entities,
        "db_terms": db_terms,
        "user_summary": _user_summary(entities),
    }


def _split_repository_paths(paths: tuple[Path, ...]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    frontend = tuple(path for path in paths if path.name in FRONTEND_REPOSITORIES)
    backend = tuple(path for path in paths if path.name in BACKEND_REPOSITORIES)
    return frontend, backend


def _derive_terms_from_matches(message: dict[str, Any], matches: list[dict[str, Any]]) -> list[str]:
    text = _combined_text(message, matches)
    terms: list[str] = []
    terms.extend(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Id|ID|Number|Status|Error)\b", text))
    terms.extend(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b", text))
    terms.extend(_entity_terms(text))
    return _unique_terms(terms)[:12]


def _infer_entities(
    message: dict[str, Any],
    frontend_matches: list[dict[str, Any]],
    backend_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text = _combined_text(message, [*frontend_matches, *backend_matches]).lower()
    entities: list[dict[str, Any]] = []
    for rule in ENTITY_RULES:
        score = sum(1 for term in (*rule.db_terms, *rule.code_terms) if term.lower() in text)
        if score:
            entities.append(
                {
                    "key": rule.key,
                    "user_label": rule.user_label,
                    "db_terms": list(rule.db_terms),
                    "confidence": min(1.0, 0.35 + score * 0.12),
                }
            )
    return sorted(entities, key=lambda item: item["confidence"], reverse=True)


def _entity_terms(text: str) -> list[str]:
    lowered = text.lower()
    terms: list[str] = []
    for rule in ENTITY_RULES:
        if any(term.lower() in lowered for term in rule.code_terms):
            terms.extend(rule.db_terms)
    return terms


def _combined_text(message: dict[str, Any], matches: list[dict[str, Any]]) -> str:
    match_text = "\n".join(str(match.get("text", "")) for match in matches)
    paths = "\n".join(str(match.get("path", "")) for match in matches)
    return f"{message_text_for_analysis(message)}\n{paths}\n{match_text}"


def _unique_terms(terms: list[str]) -> list[str]:
    unique: list[str] = []
    for term in terms:
        cleaned = str(term).strip().strip(".,;:()[]{}<>\"'")
        if len(cleaned) < 3 or cleaned.lower() in {item.lower() for item in unique}:
            continue
        unique.append(cleaned[:120])
    return unique


def _user_summary(entities: list[dict[str, Any]]) -> str:
    if not entities:
        return "По коду не удалось уверенно определить предметную сущность."
    labels = ", ".join(entity["user_label"] for entity in entities[:3])
    return f"По коду вопрос вероятнее всего относится к сущности: {labels}."

