from __future__ import annotations

import hashlib
import re
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
from xml.etree import ElementTree


MAX_CELL_SAMPLES = 160
MAX_DEFINED_NAMES = 80
MAX_INTERESTING_VALUES = 80
INTERESTING_LABELS = (
    "AgreementId",
    "Договор",
    "Договор покупки",
    "Поставщик",
    "Бренд",
    "Страна",
    "Марокко",
    "Сумма",
    "ТЭО",
    "BuyerPro",
    "БайерПРО",
    "Маршрут",
    "Логист",
    "Номенклатура",
)
ALLOWED_TEO_CURRENCIES = {
    "USD",
    "EUR",
    "RUB",
    "AMD",
    "AUD",
    "AZN",
    "BGN",
    "BRL",
    "BYN",
    "CAD",
    "CHF",
    "CNY",
    "CZK",
    "DKK",
    "GBP",
    "HKD",
    "HUF",
    "INR",
    "JPY",
    "KGS",
    "KRW",
    "KZT",
    "MDL",
    "NOK",
    "PLN",
    "RON",
    "SEK",
    "SGD",
    "TJS",
    "TMT",
    "TRY",
    "UAH",
    "UZS",
    "XDR",
    "ZAR",
}
SOURCE_REQUIRED_CELLS = {
    "B1": "поставщик ID",
    "B2": "поставщик",
    "B3": "направление закупки",
    "B4": "дата поставки",
    "B6": "валюта",
    "B7": "курс валюты",
    "B8": "страна",
    "B9": "накладные расходы",
    "B19": "тип стока",
    "B20": "тип предложения",
    "B25": "тип коробки",
    "B26": "тип подбора",
    "B27": "офис закупки",
}
SOURCE_ROW_REQUIRED_COLUMNS = {
    "A": "модель",
    "B": "бренд",
    "D": "наименование",
    "T": "товарная группа",
    "AC": "количество к заказу",
    "AM": "закупочная цена",
    "AE": "РЦ на 9",
    "AF": "РЦ целое",
}
TEO_REQUIRED_HEADER_CELLS = {
    "B1": "поставщик ID",
    "B2": "поставщик",
    "B3": "направление закупки",
    "B4": "дата поставки",
    "B5": "тип предложения",
    "B6": "тип стока",
    "D1": "менеджер",
    "D3": "страна",
    "D4": "накладные расходы",
    "D5": "курс валюты",
    "F2": "номер договора",
    "F3": "тип коробки",
    "F4": "тип подбора",
    "F6": "офис закупки",
}
TEO_ROW_REQUIRED_COLUMNS = {
    "A": "модель",
    "B": "бренд",
    "C": "наименование",
    "E": "товарная группа",
    "F": "валюта",
    "G": "количество",
    "H": "закупочная цена",
    "J": "РЦ на 9",
    "K": "РЦ целое",
}
DEFAULT_TEMPLATE_CHECK_PATH = Path(__file__).resolve().parent.parent / "templates" / "template_check.xlsx"
TEMPLATE_HEADER_ROW = 7


def inspect_buyerpro_excel_file(
    *,
    buyerpro_url: str,
    storage_path: str,
    download_dir: Path,
    max_bytes: int,
) -> dict[str, Any]:
    if not buyerpro_url:
        return {"enabled": False, "summary": "BUYERPRO_URL не задан, Excel-файл не скачивался."}
    if not storage_path:
        return {"enabled": True, "summary": "Путь к Excel-файлу пустой."}

    url = build_storage_url(buyerpro_url, storage_path)
    local_path = download_storage_file(url=url, storage_path=storage_path, download_dir=download_dir, max_bytes=max_bytes)
    parsed = parse_xlsx_xml(local_path)
    return {
        "enabled": True,
        "storage_path": storage_path,
        "url": url,
        "local_path": str(local_path),
        **parsed,
    }


def build_storage_url(buyerpro_url: str, storage_path: str) -> str:
    base = buyerpro_url.rstrip("/") + "/"
    cleaned_path = str(storage_path).strip().lstrip("/")
    if cleaned_path.startswith("storage/"):
        cleaned_path = cleaned_path.removeprefix("storage/")
    return urljoin(base, "storage/" + quote(cleaned_path, safe="/"))


def download_storage_file(*, url: str, storage_path: str, download_dir: Path, max_bytes: int) -> Path:
    import requests

    download_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(storage_path).suffix or ".xlsx"
    digest = hashlib.sha256(f"{url}|{storage_path}".encode("utf-8")).hexdigest()[:20]
    target_path = download_dir / f"{digest}{extension}"
    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")

    response = requests.get(url, stream=True, timeout=45)
    response.raise_for_status()

    written = 0
    with temp_path.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            written += len(chunk)
            if written > max_bytes:
                temp_path.unlink(missing_ok=True)
                raise ValueError(f"Excel-файл больше разрешённого лимита {max_bytes} байт")
            file.write(chunk)

    temp_path.replace(target_path)
    return target_path


def parse_xlsx_xml(
    path: Path,
    *,
    template_reference_path: Path | None = DEFAULT_TEMPLATE_CHECK_PATH,
) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheets = _read_sheet_metadata(archive)
        sheet_values = _read_sheet_values(archive, sheets, shared_strings)
        defined_names = _read_defined_names(archive, sheet_values)
        samples = _cell_samples(sheet_values)
        interesting_values = _interesting_values(sheet_values)
        download_teo_checks = _download_teo_checks(
            sheets,
            sheet_values,
            template_reference_path=template_reference_path,
        )

        return {
            "summary": _summary(path, sheets, defined_names, interesting_values, samples, download_teo_checks),
            "xml_parts": _xml_parts(archive),
            "sheets": [{"name": sheet["name"], "path": sheet["path"]} for sheet in sheets],
            "defined_names": defined_names,
            "interesting_values": interesting_values,
            "cell_samples": samples,
            "download_teo_checks": download_teo_checks,
        }


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall(".//{*}si"):
        chunks = [node.text or "" for node in item.findall(".//{*}t")]
        strings.append("".join(chunks))
    return strings


def _read_sheet_metadata(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    if "xl/workbook.xml" not in archive.namelist():
        return []

    rels = _workbook_relationships(archive)
    root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheets: list[dict[str, str]] = []
    for sheet in root.findall(".//{*}sheet"):
        name = sheet.attrib.get("name", "sheet")
        relationship_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rels.get(relationship_id, "")
        path = _normalize_workbook_target(target)
        if path:
            sheets.append({"name": name, "path": path})
    return sheets


def _workbook_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in archive.namelist():
        return {}
    root = ElementTree.fromstring(archive.read(rels_path))
    return {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in root.findall(".//{*}Relationship")
    }


def _normalize_workbook_target(target: str) -> str:
    if not target:
        return ""
    cleaned = target.lstrip("/")
    if cleaned.startswith("xl/"):
        return cleaned
    return f"xl/{cleaned}"


def _read_sheet_values(
    archive: zipfile.ZipFile,
    sheets: list[dict[str, str]],
    shared_strings: list[str],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for sheet in sheets:
        path = sheet["path"]
        if path not in archive.namelist():
            continue
        root = ElementTree.fromstring(archive.read(path))
        cells: dict[str, str] = {}
        for cell in root.findall(".//{*}c"):
            reference = cell.attrib.get("r", "")
            if not reference:
                continue
            value = _cell_value(cell, shared_strings)
            if value != "":
                cells[reference] = value
        result[sheet["name"]] = cells
    return result


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    inline_text = cell.find(".//{*}is/{*}t")
    if inline_text is not None:
        return inline_text.text or ""

    value_node = cell.find("{*}v")
    if value_node is None or value_node.text is None:
        return ""

    raw_value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (ValueError, IndexError):
            return raw_value
    return raw_value


def _read_defined_names(
    archive: zipfile.ZipFile,
    sheet_values: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    if "xl/workbook.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    names: list[dict[str, str]] = []
    for item in root.findall(".//{*}definedName")[:MAX_DEFINED_NAMES]:
        name = item.attrib.get("name", "")
        target = item.text or ""
        value = _defined_name_value(target, sheet_values)
        names.append({"name": name, "target": target, "value": value})
    return names


def _defined_name_value(target: str, sheet_values: dict[str, dict[str, str]]) -> str:
    match = re.match(r"'?([^'!]+)'?!\$?([A-Z]+)\$?(\d+)$", target)
    if not match:
        return ""
    sheet_name = match.group(1)
    cell_ref = f"{match.group(2)}{match.group(3)}"
    return sheet_values.get(sheet_name, {}).get(cell_ref, "")


def _cell_samples(sheet_values: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for sheet_name, cells in sheet_values.items():
        for cell_ref in sorted(cells, key=_cell_sort_key):
            samples.append({"sheet": sheet_name, "cell": cell_ref, "value": cells[cell_ref]})
            if len(samples) >= MAX_CELL_SAMPLES:
                return samples
    return samples


def _interesting_values(sheet_values: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    lowered_labels = tuple(label.lower() for label in INTERESTING_LABELS)
    for sheet_name, cells in sheet_values.items():
        for cell_ref, value in cells.items():
            lowered_value = value.lower()
            if not any(label in lowered_value for label in lowered_labels):
                continue
            right_value = cells.get(_next_column_ref(cell_ref), "")
            values.append({"sheet": sheet_name, "cell": cell_ref, "label": value, "value_right": right_value})
            if len(values) >= MAX_INTERESTING_VALUES:
                return values
    return values


def _download_teo_checks(
    sheets: list[dict[str, str]],
    sheet_values: dict[str, dict[str, str]],
    *,
    template_reference_path: Path | None,
) -> dict[str, Any]:
    sheet_names = [sheet["name"] for sheet in sheets]
    intro_sheet = _sheet_values_by_name(sheet_values, "вводные")
    source_template_sheet = _sheet_values_by_name(sheet_values, "шаблон")
    teo_sheet = _sheet_values_by_name(sheet_values, "тэо")

    checks: list[dict[str, Any]] = []
    if intro_sheet is not None or source_template_sheet is not None:
        workbook_type = "converter_source"
        checks.extend(_source_workbook_checks(intro_sheet, source_template_sheet, template_reference_path))
    elif teo_sheet is not None:
        workbook_type = "teo_result"
        checks.extend(_teo_result_checks(teo_sheet))
    else:
        workbook_type = "unknown"
        checks.append(
            _check(
                "expected_download_teo_sheets",
                "failed",
                "Не найдены листы `вводные` + `Шаблон` или итоговый лист `ТЭО`.",
                {"sheets": sheet_names},
            )
        )

    counts = _check_status_counts(checks)
    failed_details = _failed_check_messages(checks)
    return {
        "summary": _download_teo_summary(workbook_type, counts, failed_details),
        "detected_workbook_type": workbook_type,
        "checks": checks,
        "status_counts": counts,
    }


def _source_workbook_checks(
    intro_sheet: dict[str, str] | None,
    template_sheet: dict[str, str] | None,
    template_reference_path: Path | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    missing_sheets = []
    if intro_sheet is None:
        missing_sheets.append("вводные")
    if template_sheet is None:
        missing_sheets.append("Шаблон")
    checks.append(
        _check(
            "source_required_sheets",
            "failed" if missing_sheets else "passed",
            (
                "Для ветки `normal` в `download/teo` нужны листы `вводные` и `Шаблон`."
                if missing_sheets
                else "Найдены листы, которые читает `download/teo` для исходного файла предложения."
            ),
            {"missing_sheets": missing_sheets},
        )
    )

    if intro_sheet is not None:
        missing_cells = [
            {"cell": cell_ref, "field": label}
            for cell_ref, label in SOURCE_REQUIRED_CELLS.items()
            if not _sheet_cell_value(intro_sheet, cell_ref)
        ]
        checks.append(
            _check(
                "source_intro_required_cells",
                "failed" if missing_cells else "passed",
                (
                    "`teoHelper` берёт эти поля с листа `вводные` для шапки ТЭО."
                    if missing_cells
                    else "Ключевые поля листа `вводные` заполнены."
                ),
                {"missing_cells": missing_cells[:30]},
            )
        )

        numeric_issues = []
        for cell_ref, label in {"B7": "курс валюты", "B9": "накладные расходы"}.items():
            value = _sheet_cell_value(intro_sheet, cell_ref)
            if value and not _is_number(value):
                numeric_issues.append({"cell": cell_ref, "field": label, "value": value})
        checks.append(
            _check(
                "source_intro_numeric_fields",
                "warning" if numeric_issues else "passed",
                (
                    "Поля, которые попадут в числовые ячейки ТЭО, выглядят нечисловыми."
                    if numeric_issues
                    else "Курс валюты и накладные расходы выглядят числовыми."
                ),
                {"issues": numeric_issues},
            )
        )

        country = _sheet_cell_value(intro_sheet, "B8").strip().lower()
        if country and country != "россия":
            missing_logistics = [
                {"cell": "C2", "field": "логистическая схема"},
                {"cell": "C4", "field": "дата готовности товара"},
            ]
            missing_logistics = [item for item in missing_logistics if not _sheet_cell_value(intro_sheet, item["cell"])]
            checks.append(
                _check(
                    "source_non_russia_logistics",
                    "warning" if missing_logistics else "passed",
                    (
                        "`download/teo` переносит логистическую схему и дату готовности для стран не Россия."
                        if missing_logistics
                        else "Для страны не Россия заполнены логистическая схема и дата готовности."
                    ),
                    {"missing_cells": missing_logistics},
                )
            )

    if template_sheet is not None:
        checks.append(_source_template_reference_columns_check(template_sheet, template_reference_path))

        rows = _source_template_rows(template_sheet)
        checks.append(
            _check(
                "source_template_item_rows",
                "failed" if rows["accepted_rows"] == 0 else "passed",
                (
                    _source_template_item_rows_failure_message(rows)
                    if rows["accepted_rows"] == 0
                    else f"Найдено строк, которые `teoHelper` перенесёт в ТЭО: {rows['accepted_rows']}."
                ),
                rows,
            )
        )

        row_issues = _source_template_row_issues(template_sheet, rows["accepted_row_numbers"])
        checks.append(
            _check(
                "source_template_required_columns",
                "warning" if row_issues else "passed",
                (
                    "В строках, которые попадут в ТЭО, есть пустые/нечисловые ключевые колонки."
                    if row_issues
                    else "Ключевые колонки строк предложения заполнены."
                ),
                {"issues": row_issues[:40]},
            )
        )

    return checks


def _teo_result_checks(teo_sheet: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    missing_cells = [
        {"cell": cell_ref, "field": label}
        for cell_ref, label in TEO_REQUIRED_HEADER_CELLS.items()
        if not _sheet_cell_value(teo_sheet, cell_ref)
    ]
    checks.append(
        _check(
            "teo_header_required_cells",
            "failed" if missing_cells else "passed",
            (
                "Итоговый лист `ТЭО` не содержит часть обязательных полей шапки."
                if missing_cells
                else "Обязательные поля шапки итогового листа `ТЭО` заполнены."
            ),
            {"missing_cells": missing_cells[:30]},
        )
    )

    header_issues = []
    for cell_ref, label in {"D4": "накладные расходы", "D5": "курс валюты"}.items():
        value = _sheet_cell_value(teo_sheet, cell_ref)
        if value and not _is_number(value):
            header_issues.append({"cell": cell_ref, "field": label, "value": value})
    manager = _sheet_cell_value(teo_sheet, "D1")
    if manager and not re.fullmatch(r"[A-Za-z.]+", manager):
        header_issues.append({"cell": "D1", "field": "менеджер", "value": manager})
    checks.append(
        _check(
            "teo_header_format",
            "warning" if header_issues else "passed",
            (
                "Часть полей шапки не проходит форматные проверки `TeoCheckUsecases`."
                if header_issues
                else "Числовые поля шапки и формат менеджера выглядят корректно."
            ),
            {"issues": header_issues},
        )
    )

    country = _sheet_cell_value(teo_sheet, "D3").strip().lower()
    if country and country != "россия":
        missing_logistics = [
            {"cell": "J6", "field": "логистическая схема"},
            {"cell": "L1", "field": "дата прихода на РЦ"},
        ]
        missing_logistics = [item for item in missing_logistics if not _sheet_cell_value(teo_sheet, item["cell"])]
        checks.append(
            _check(
                "teo_non_russia_logistics",
                "warning" if missing_logistics else "passed",
                (
                    "`TeoCheckUsecases` требует логистическую схему и дату прихода на РЦ для стран не Россия."
                    if missing_logistics
                    else "Для страны не Россия заполнены логистическая схема и дата прихода на РЦ."
                ),
                {"missing_cells": missing_logistics},
            )
        )

    row_numbers = _teo_row_numbers(teo_sheet)
    checks.append(
        _check(
            "teo_item_rows",
            "failed" if not row_numbers else "passed",
            (
                "На листе `ТЭО` не найдены товарные строки, которые проверяет `TeoCheckUsecases`."
                if not row_numbers
                else f"Найдено товарных строк на листе `ТЭО`: {len(row_numbers)}."
            ),
            {"row_count": len(row_numbers), "sample_rows": row_numbers[:20]},
        )
    )

    row_issues = _teo_row_issues(teo_sheet, row_numbers)
    checks.append(
        _check(
            "teo_required_row_fields",
            "warning" if row_issues else "passed",
            (
                "В товарных строках есть пустые/нечисловые поля, похожие на ошибки из `TeoCheckUsecases`."
                if row_issues
                else "Ключевые поля товарных строк итогового ТЭО заполнены."
            ),
            {"issues": row_issues[:60]},
        )
    )
    return checks


def _source_template_reference_columns_check(
    template_sheet: dict[str, str],
    template_reference_path: Path | None,
) -> dict[str, Any]:
    if template_reference_path is None:
        return _check(
            "source_template_reference_columns",
            "warning",
            "Эталонный файл `template_check.xlsx` не задан, колонки листа `Шаблон` не сравнивались.",
            {},
        )

    try:
        reference_template_sheet = _read_reference_template_sheet(template_reference_path)
    except Exception as exc:
        return _check(
            "source_template_reference_columns",
            "warning",
            f"Не удалось прочитать эталонный файл `{template_reference_path}`: {exc}",
            {"reference_path": str(template_reference_path)},
        )

    if reference_template_sheet is None:
        return _check(
            "source_template_reference_columns",
            "warning",
            "В эталонном файле не найден лист `Шаблон`, колонки не сравнивались.",
            {"reference_path": str(template_reference_path)},
        )

    expected_columns = _template_header_columns(reference_template_sheet)
    actual_columns = _template_header_columns(template_sheet)
    comparison = _compare_template_header_columns(expected_columns, actual_columns)
    missing_columns = comparison["missing_columns"]
    mismatched_columns = comparison["mismatched_columns"]
    status = "failed" if missing_columns or mismatched_columns else "passed"
    issue_summary = _template_column_issue_summary(missing_columns, mismatched_columns)

    return _check(
        "source_template_reference_columns",
        status,
        (
            f"Колонки листа `Шаблон` отличаются от эталона `template_check.xlsx`: {issue_summary}"
            if status == "failed"
            else "Колонки листа `Шаблон` сверены с эталоном `template_check.xlsx`."
        ),
        {
            "reference_path": str(template_reference_path),
            "header_row": TEMPLATE_HEADER_ROW,
            "expected_column_count": len(expected_columns),
            "actual_column_count": len(actual_columns),
            "issue_summary": issue_summary,
            "missing_columns": missing_columns[:80],
            "mismatched_columns": mismatched_columns[:80],
            "ignored_extra_columns": comparison["ignored_extra_columns"][:40],
        },
    )


def _compare_template_header_columns(
    expected_columns: dict[str, str],
    actual_columns: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    expected_items = list(expected_columns.items())
    actual_items = list(actual_columns.items())
    expected_headers = [_normalize_header(header) for _, header in expected_items]
    actual_headers = [_normalize_header(header) for _, header in actual_items]

    missing_columns: list[dict[str, str]] = []
    mismatched_columns: list[dict[str, str]] = []
    ignored_extra_columns: list[dict[str, str]] = []

    matcher = SequenceMatcher(a=expected_headers, b=actual_headers, autojunk=False)
    for tag, expected_start, expected_end, actual_start, actual_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            missing_columns.extend(
                {"column": column, "expected": header}
                for column, header in expected_items[expected_start:expected_end]
            )
            continue
        if tag == "insert":
            ignored_extra_columns.extend(
                {"column": column, "actual": header}
                for column, header in actual_items[actual_start:actual_end]
            )
            continue

        expected_slice = expected_items[expected_start:expected_end]
        actual_slice = actual_items[actual_start:actual_end]
        paired_count = min(len(expected_slice), len(actual_slice))
        for index in range(paired_count):
            expected_column, expected_header = expected_slice[index]
            _, actual_header = actual_slice[index]
            mismatched_columns.append(
                {
                    "column": expected_column,
                    "expected": expected_header,
                    "actual": actual_header,
                }
            )
        for expected_column, expected_header in expected_slice[paired_count:]:
            missing_columns.append({"column": expected_column, "expected": expected_header})
        ignored_extra_columns.extend(
            {"column": actual_column, "actual": actual_header}
            for actual_column, actual_header in actual_slice[paired_count:]
        )

    return {
        "missing_columns": missing_columns,
        "mismatched_columns": mismatched_columns,
        "ignored_extra_columns": ignored_extra_columns,
    }


def _template_column_issue_summary(
    missing_columns: list[dict[str, str]],
    mismatched_columns: list[dict[str, str]],
) -> str:
    parts = []
    if missing_columns:
        parts.append(
            "пропущены/удалены "
            + "; ".join(
                f"{item['column']} `{item['expected']}`"
                for item in missing_columns[:20]
            )
        )
    if mismatched_columns:
        parts.append(
            "отличаются названия "
            + "; ".join(
                f"{item['column']}: ожидалось `{item['expected']}`, в файле `{item['actual']}`"
                for item in mismatched_columns[:20]
            )
        )
    return "; ".join(parts)


def _read_reference_template_sheet(path: Path) -> dict[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheets = _read_sheet_metadata(archive)
        sheet_values = _read_sheet_values(archive, sheets, shared_strings)
    return _sheet_values_by_name(sheet_values, "шаблон")


def _template_header_columns(cells: dict[str, str]) -> dict[str, str]:
    columns: dict[str, str] = {}
    for cell_ref, value in cells.items():
        match = re.fullmatch(r"([A-Z]+)(\d+)", cell_ref)
        if not match or int(match.group(2)) != TEMPLATE_HEADER_ROW:
            continue
        cleaned = str(value or "").strip()
        if cleaned:
            columns[match.group(1)] = cleaned
    return dict(sorted(columns.items(), key=lambda item: _column_index(item[0])))


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).replace("ё", "е").strip().lower())


def _source_template_rows(cells: dict[str, str]) -> dict[str, Any]:
    accepted_row_numbers: list[int] = []
    quantity_issues: list[dict[str, Any]] = []
    skipped_by_quantity = 0
    skip_count = 0
    rows_with_model = 0
    headers = _template_header_columns(cells)
    for row_number in range(8, _max_row(cells) + 1):
        model = _sheet_cell_value(cells, f"A{row_number}")
        if not model:
            skip_count += 1
            if skip_count >= 4:
                break
            continue
        rows_with_model += 1
        quantity = _sheet_cell_value(cells, f"AC{row_number}")
        if not _is_positive_int(quantity):
            skipped_by_quantity += 1
            if len(quantity_issues) < 20:
                quantity_issues.append(
                    {
                        "row": row_number,
                        "model_column": "A",
                        "model_header": headers.get("A", ""),
                        "model_value": model,
                        "quantity_column": "AC",
                        "quantity_header": headers.get("AC", ""),
                        "quantity_value": quantity,
                        "neighbor_quantity_column": "AB",
                        "neighbor_quantity_header": headers.get("AB", ""),
                        "neighbor_quantity_value": _sheet_cell_value(cells, f"AB{row_number}"),
                    }
                )
            continue
        accepted_row_numbers.append(row_number)

    return {
        "accepted_rows": len(accepted_row_numbers),
        "accepted_row_numbers": accepted_row_numbers[:80],
        "rows_with_model": rows_with_model,
        "skipped_by_empty_or_zero_quantity": skipped_by_quantity,
        "quantity_issues": quantity_issues,
    }


def _source_template_item_rows_failure_message(rows: dict[str, Any]) -> str:
    if rows.get("rows_with_model", 0) > 0 and rows.get("skipped_by_empty_or_zero_quantity", 0) > 0:
        return (
            "На листе `Шаблон` найдены строки с заполненной колонкой A, но `teoHelper` не перенесёт их: "
            "в колонке AC `F*Заказ шт` нет положительного числа. "
            "Если количество заполнено в AB `V*Количество`, этого недостаточно: выгрузка читает именно AC."
        )
    return "`teoHelper` переносит только строки с заполненной колонкой A и числовой AC > 0."


def _source_template_row_issues(cells: dict[str, str], row_numbers: list[int]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row_number in row_numbers[:80]:
        for column, label in SOURCE_ROW_REQUIRED_COLUMNS.items():
            value = _sheet_cell_value(cells, f"{column}{row_number}")
            if not value:
                issues.append({"row": row_number, "column": column, "field": label, "problem": "empty"})
                continue
            if column in {"AC"} and not _is_positive_int(value):
                issues.append(
                    {"row": row_number, "column": column, "field": label, "problem": "not_positive_int", "value": value}
                )
            if column in {"AM", "AE", "AF"} and not _is_number(value):
                issues.append(
                    {"row": row_number, "column": column, "field": label, "problem": "not_number", "value": value}
                )
    return issues


def _teo_row_numbers(cells: dict[str, str]) -> list[int]:
    row_numbers: list[int] = []
    skip_count = 0
    for row_number in range(10, _max_row(cells) + 1):
        if not any(_sheet_cell_value(cells, f"{column}{row_number}") for column in ("A", "B", "C")):
            skip_count += 1
            if skip_count >= 4:
                break
            continue
        row_numbers.append(row_number)
    return row_numbers[:120]


def _teo_row_issues(cells: dict[str, str], row_numbers: list[int]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row_number in row_numbers[:120]:
        for column, label in TEO_ROW_REQUIRED_COLUMNS.items():
            value = _sheet_cell_value(cells, f"{column}{row_number}")
            if not value:
                issues.append({"row": row_number, "column": column, "field": label, "problem": "empty"})
                continue
            if column == "F" and value.strip().upper() not in ALLOWED_TEO_CURRENCIES:
                issues.append(
                    {"row": row_number, "column": column, "field": label, "problem": "unknown_currency", "value": value}
                )
            if column in {"G"} and not _is_positive_int(value):
                issues.append(
                    {"row": row_number, "column": column, "field": label, "problem": "not_positive_int", "value": value}
                )
            if column in {"H", "J", "K"} and not _is_number(value):
                issues.append(
                    {"row": row_number, "column": column, "field": label, "problem": "not_number", "value": value}
                )
    return issues


def _sheet_values_by_name(sheet_values: dict[str, dict[str, str]], expected_name: str) -> dict[str, str] | None:
    expected = expected_name.lower()
    for sheet_name, cells in sheet_values.items():
        if sheet_name.strip().lower() == expected:
            return cells
    return None


def _sheet_cell_value(cells: dict[str, str], cell_ref: str) -> str:
    return str(cells.get(cell_ref) or "").strip()


def _max_row(cells: dict[str, str]) -> int:
    rows = []
    for cell_ref in cells:
        match = re.match(r"[A-Z]+(\d+)$", cell_ref)
        if match:
            rows.append(int(match.group(1)))
    return max(rows, default=0)


def _column_index(column: str) -> int:
    index = 0
    for char in column:
        index = index * 26 + ord(char) - ord("A") + 1
    return index


def _is_number(value: str) -> bool:
    try:
        float(str(value).strip().replace(",", "."))
    except ValueError:
        return False
    return True


def _is_positive_int(value: str) -> bool:
    try:
        return int(float(str(value).strip().replace(",", "."))) > 0
    except ValueError:
        return False


def _check(name: str, status: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "details": details}


def _check_status_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "failed": sum(1 for check in checks if check.get("status") == "failed"),
        "warning": sum(1 for check in checks if check.get("status") == "warning"),
        "passed": sum(1 for check in checks if check.get("status") == "passed"),
    }


def _failed_check_messages(checks: list[dict[str, Any]]) -> list[str]:
    return [
        str(check["message"])
        for check in checks
        if check.get("status") == "failed" and check.get("message")
    ][:4]


def _download_teo_summary(workbook_type: str, counts: dict[str, int], failed_details: list[str] | None = None) -> str:
    workbook_type_label = {
        "converter_source": "исходный файл предложения для `download/teo`",
        "teo_result": "итоговый файл ТЭО",
        "unknown": "неизвестный тип XLSX",
    }.get(workbook_type, workbook_type)
    if counts["failed"]:
        status = f"есть критичные проблемы: {counts['failed']}"
    elif counts["warning"]:
        status = f"есть предупреждения: {counts['warning']}"
    else:
        status = "критичных проблем не найдено"
    summary = f"Проверки совместимости с `download/teo`: {workbook_type_label}, {status}."
    if failed_details:
        summary += " Критичные проверки: " + "; ".join(failed_details) + "."
    return summary


def _next_column_ref(cell_ref: str) -> str:
    match = re.match(r"([A-Z]+)(\d+)$", cell_ref)
    if not match:
        return ""
    return f"{_next_column(match.group(1))}{match.group(2)}"


def _next_column(column: str) -> str:
    number = 0
    for char in column:
        number = number * 26 + (ord(char) - ord("A") + 1)
    number += 1
    chars: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _cell_sort_key(cell_ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)(\d+)$", cell_ref)
    if not match:
        return (10**9, 10**9)
    column = 0
    for char in match.group(1):
        column = column * 26 + (ord(char) - ord("A") + 1)
    return (int(match.group(2)), column)


def _xml_parts(archive: zipfile.ZipFile) -> list[str]:
    return [name for name in archive.namelist() if name.endswith(".xml")][:120]


def _summary(
    path: Path,
    sheets: list[dict[str, str]],
    defined_names: list[dict[str, str]],
    interesting_values: list[dict[str, str]],
    samples: list[dict[str, str]],
    download_teo_checks: dict[str, Any],
) -> str:
    return (
        f"Excel-файл {path.name} распаршен как XLSX/XML: листов {len(sheets)}, "
        f"именованных диапазонов {len(defined_names)}, интересных значений {len(interesting_values)}, "
        f"примеров непустых ячеек {len(samples)}. "
        f"{download_teo_checks.get('summary', '')}"
    )
