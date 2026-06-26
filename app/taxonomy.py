from __future__ import annotations

from enum import Enum


class ProblemCategory(str, Enum):
    EXCEL = "excel"
    SHAREPOINT = "sharepoint"
    AXAPTA = "axapta"
    BUYERPRO = "buyerpro"
    INTEGRATIONS = "integrations"
    OTHER = "other"


CATEGORY_LABELS: dict[ProblemCategory, str] = {
    ProblemCategory.EXCEL: "Excel",
    ProblemCategory.SHAREPOINT: "Sharepoint",
    ProblemCategory.AXAPTA: "Аксапта",
    ProblemCategory.BUYERPRO: "БайерПро",
    ProblemCategory.INTEGRATIONS: "Интеграции",
    ProblemCategory.OTHER: "Другое",
}


CATEGORY_KEYWORDS: dict[ProblemCategory, tuple[str, ...]] = {
    ProblemCategory.EXCEL: (
        "excel",
        "xlsx",
        "xls",
        "таблица",
        "выгрузка",
        "импорт",
        "экспорт",
        "файл",
    ),
    ProblemCategory.SHAREPOINT: (
        "sharepoint",
        "onedrive",
        "teams",
        "документ",
        "ссылка",
        "папка",
        "доступ",
    ),
    ProblemCategory.AXAPTA: (
        "axapta",
        "аксапта",
        "аксанта",
        "dynamics",
        "erp",
        "заказ",
        "номенклатура",
    ),
    ProblemCategory.BUYERPRO: (
        "buyerpro",
        "байерпро",
        "buyer pro",
        "pro.famil",
        "market.famil",
        "кабинет",
        "поставщик",
    ),
    ProblemCategory.INTEGRATIONS: (
        "интеграция",
        "интеграции",
        "api",
        "обмен",
        "синхронизация",
        "webhook",
        "endpoint",
        "ошибка обмена",
    ),
}


def normalize_category(value: str | None) -> ProblemCategory:
    if not value:
        return ProblemCategory.OTHER

    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "excel": ProblemCategory.EXCEL,
        "эксель": ProblemCategory.EXCEL,
        "sharepoint": ProblemCategory.SHAREPOINT,
        "share_point": ProblemCategory.SHAREPOINT,
        "аксапта": ProblemCategory.AXAPTA,
        "аксанта": ProblemCategory.AXAPTA,
        "axapta": ProblemCategory.AXAPTA,
        "байерпро": ProblemCategory.BUYERPRO,
        "buyerpro": ProblemCategory.BUYERPRO,
        "buyer_pro": ProblemCategory.BUYERPRO,
        "интеграции": ProblemCategory.INTEGRATIONS,
        "интеграция": ProblemCategory.INTEGRATIONS,
        "integrations": ProblemCategory.INTEGRATIONS,
        "integration": ProblemCategory.INTEGRATIONS,
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

