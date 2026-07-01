from __future__ import annotations

from enum import Enum


class ProblemCategory(str, Enum):
    CONVERTER_OFFERS = "converter_offers"
    TEO_APPROVAL = "teo_approval"
    OTHER = "other"


CATEGORY_LABELS: dict[ProblemCategory, str] = {
    ProblemCategory.CONVERTER_OFFERS: "Конвертер/Список предложений",
    ProblemCategory.TEO_APPROVAL: "Согласование ТЭО",
    ProblemCategory.OTHER: "Другое",
}


CATEGORY_KEYWORDS: dict[ProblemCategory, tuple[str, ...]] = {
    ProblemCategory.CONVERTER_OFFERS: (
        "конвертер",
        "список предложений",
        "номер предложения",
        "предложение",
        "converter",
        "brandid",
        "converter_id",
        "buyerpro",
        "байерпро",
        "buyer pro",
        "pro.famil",
        "market.famil",
        "кабинет",
        "поставщик",
        "excel",
        "xlsx",
        "xls",
        "выгрузка",
        "импорт",
        "экспорт",
        "файл",
    ),
    ProblemCategory.TEO_APPROVAL: (
        "согласование тэо",
        "согласование тео",
        "тэо",
        "тео",
        "на согласовании",
        "ожидает согласования",
        "согласование",
        "согласовать",
        "согласующий",
        "доработке",
        "заявка на закупку",
        "purch_req_request",
        "acsapta_teo",
        "acsaptateo",
        "approve",
        "approval",
        "approver",
        "permission_rule",
    ),
}


def normalize_category(value: str | None) -> ProblemCategory:
    if not value:
        return ProblemCategory.OTHER

    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "конвертер": ProblemCategory.CONVERTER_OFFERS,
        "список предложений": ProblemCategory.CONVERTER_OFFERS,
        "конвертер/список предложений": ProblemCategory.CONVERTER_OFFERS,
        "converter": ProblemCategory.CONVERTER_OFFERS,
        "converter_offers": ProblemCategory.CONVERTER_OFFERS,
        "offers": ProblemCategory.CONVERTER_OFFERS,
        "buyerpro": ProblemCategory.CONVERTER_OFFERS,
        "buyer_pro": ProblemCategory.CONVERTER_OFFERS,
        "байерпро": ProblemCategory.CONVERTER_OFFERS,
        "согласование тэо": ProblemCategory.TEO_APPROVAL,
        "согласование тео": ProblemCategory.TEO_APPROVAL,
        "teo_approval": ProblemCategory.TEO_APPROVAL,
        "teo": ProblemCategory.TEO_APPROVAL,
        "тэо": ProblemCategory.TEO_APPROVAL,
        "тео": ProblemCategory.TEO_APPROVAL,
        "другое": ProblemCategory.OTHER,
        "other": ProblemCategory.OTHER,
    }
    if normalized in aliases:
        return aliases[normalized]

    for category in ProblemCategory:
        if normalized == category.value:
            return category

    return ProblemCategory.OTHER


def category_label(category: ProblemCategory | str | None) -> str:
    normalized = normalize_category(category if isinstance(category, str) else category.value if category else None)
    return CATEGORY_LABELS[normalized]


def guess_category(subject: str, body: str) -> ProblemCategory:
    text = f"{subject}\n{body}".lower()
    scores: dict[ProblemCategory, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        scores[category] = sum(1 for keyword in keywords if keyword in text)

    best_category = max(scores, key=scores.get)
    if scores[best_category] == 0:
        return ProblemCategory.OTHER
    return best_category

