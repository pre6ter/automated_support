from __future__ import annotations

import hashlib
import re
import zipfile
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
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    response = requests.get(url, stream=True, timeout=45)
    response.raise_for_status()

    written = 0
    with target_path.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            written += len(chunk)
            if written > max_bytes:
                target_path.unlink(missing_ok=True)
                raise ValueError(f"Excel-файл больше разрешённого лимита {max_bytes} байт")
            file.write(chunk)

    return target_path


def parse_xlsx_xml(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheets = _read_sheet_metadata(archive)
        sheet_values = _read_sheet_values(archive, sheets, shared_strings)
        defined_names = _read_defined_names(archive, sheet_values)
        samples = _cell_samples(sheet_values)
        interesting_values = _interesting_values(sheet_values)

        return {
            "summary": _summary(path, sheets, defined_names, interesting_values, samples),
            "xml_parts": _xml_parts(archive),
            "sheets": [{"name": sheet["name"], "path": sheet["path"]} for sheet in sheets],
            "defined_names": defined_names,
            "interesting_values": interesting_values,
            "cell_samples": samples,
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
) -> str:
    return (
        f"Excel-файл {path.name} распаршен как XLSX/XML: листов {len(sheets)}, "
        f"именованных диапазонов {len(defined_names)}, интересных значений {len(interesting_values)}, "
        f"примеров непустых ячеек {len(samples)}."
    )
