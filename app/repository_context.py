from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config
from app.domain_knowledge import offer_number_terms
from app.support_issue_parser import message_text_for_analysis
from app.taxonomy import CATEGORY_KEYWORDS, ProblemCategory, normalize_category


MAX_TERMS = 14
MAX_LINE_LENGTH = 260


@dataclass(frozen=True)
class RepositoryMatch:
    repository: str
    path: str
    line: int
    term: str
    text: str


def collect_repository_context(
    config: Config,
    message: dict[str, Any],
    category: ProblemCategory,
) -> dict[str, Any]:
    terms = extract_search_terms(message, category)
    return search_repository_paths(config.repository_paths, terms, config.repository_search_limit)


def search_repository_paths(repository_paths: tuple[Path, ...], terms: list[str], search_limit: int) -> dict[str, Any]:
    matches: list[RepositoryMatch] = []
    errors: list[str] = []

    for repository_path in repository_paths:
        if not repository_path.exists():
            errors.append(f"{repository_path}: путь не найден")
            continue
        if not repository_path.is_dir():
            errors.append(f"{repository_path}: не директория")
            continue

        for term in terms:
            matches.extend(_search_repository(repository_path, term, search_limit))

    return {
        "terms": terms,
        "matches": [match.__dict__ for match in matches[: search_limit * max(len(repository_paths), 1)]],
        "errors": errors,
    }


def extract_search_terms(message: dict[str, Any], category: ProblemCategory | str) -> list[str]:
    normalized_category = normalize_category(category.value if isinstance(category, ProblemCategory) else category)
    text = message_text_for_analysis(message)
    terms: list[str] = []

    terms.extend(offer_number_terms(text))
    terms.extend(_interesting_fragments(text))
    terms.extend(CATEGORY_KEYWORDS.get(normalized_category, ())[:3])

    unique_terms: list[str] = []
    for term in terms:
        cleaned = _clean_term(term)
        if not cleaned or cleaned.lower() in {item.lower() for item in unique_terms}:
            continue
        unique_terms.append(cleaned)
        if len(unique_terms) >= MAX_TERMS:
            break
    return unique_terms


def _interesting_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    fragments.extend(re.findall(r"https?://[^\s)\]>\"']+", text))
    fragments.extend(re.findall(r"\b[A-Z][A-Z0-9_/-]{2,}\b", text))
    fragments.extend(re.findall(r"\b(?:error|exception|failed|timeout|ошибка|исключение|не работает)\b.{0,80}", text, re.I))
    fragments.extend(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{4,}", text))
    return fragments


def _clean_term(term: str) -> str:
    cleaned = term.strip().strip(".,;:()[]{}<>\"'")
    if len(cleaned) < 4:
        return ""
    return cleaned[:120]


def _search_repository(repository_path: Path, term: str, limit: int) -> list[RepositoryMatch]:
    command = [
        "rg",
        "--fixed-strings",
        "--ignore-case",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--max-count",
        str(max(limit, 1)),
        term,
        str(repository_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode not in {0, 1}:
        return []

    matches: list[RepositoryMatch] = []
    for raw_line in result.stdout.splitlines()[:limit]:
        parsed = _parse_rg_line(raw_line, repository_path, term)
        if parsed:
            matches.append(parsed)
    return matches


def _parse_rg_line(raw_line: str, repository_path: Path, term: str) -> RepositoryMatch | None:
    parts = raw_line.split(":", 2)
    if len(parts) != 3:
        return None
    file_path, line_number, text = parts
    try:
        line = int(line_number)
    except ValueError:
        return None

    path = Path(file_path)
    try:
        relative_path = str(path.relative_to(repository_path))
    except ValueError:
        relative_path = str(path)

    return RepositoryMatch(
        repository=repository_path.name,
        path=relative_path,
        line=line,
        term=term,
        text=text.strip()[:MAX_LINE_LENGTH],
    )

