from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from werkzeug.datastructures import FileStorage


SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_EXTENSIONS = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class ImageAttachment:
    filename: str
    content_type: str
    path: str
    size: int


def save_email_attachments(
    storage_dir: Path,
    mail_id: str,
    parts: list[dict[str, Any]],
    max_bytes: int,
) -> list[ImageAttachment]:
    attachments: list[ImageAttachment] = []
    for index, part in enumerate(parts, start=1):
        content_type = str(part.get("content_type") or "application/octet-stream")
        payload = part.get("payload") or b""
        if not _is_allowed_attachment(len(payload), max_bytes):
            continue

        filename = _safe_filename(str(part.get("filename") or f"attachment-{index}{_extension(content_type)}"))
        path = _write_attachment(storage_dir / "mail" / mail_id, filename, payload)
        attachments.append(ImageAttachment(filename=filename, content_type=content_type, path=str(path), size=len(payload)))
    return attachments


def save_email_images(storage_dir: Path, mail_id: str, parts: list[dict[str, Any]], max_bytes: int) -> list[ImageAttachment]:
    return [
        attachment
        for attachment in save_email_attachments(storage_dir, mail_id, parts, max_bytes)
        if is_image_attachment(attachment)
    ]


def save_chat_uploads(storage_dir: Path, files: list["FileStorage"], max_bytes: int) -> list[ImageAttachment]:
    attachments: list[ImageAttachment] = []
    for index, file in enumerate(files, start=1):
        payload = file.read()
        content_type = file.mimetype or ""
        if not _is_supported_image(content_type, len(payload), max_bytes):
            continue

        filename = _safe_filename(file.filename or f"chat-image-{index}{_extension(content_type)}")
        digest = hashlib.sha256(payload).hexdigest()[:16]
        path = _write_image(storage_dir / "chat" / digest, filename, payload)
        attachments.append(ImageAttachment(filename=filename, content_type=content_type, path=str(path), size=len(payload)))
    return attachments


def image_to_openai_part(image: dict[str, Any] | ImageAttachment) -> dict[str, Any]:
    attachment = _as_attachment(image)
    if not is_image_attachment(attachment):
        raise ValueError("Only image attachments can be passed to vision models")
    content_type = infer_image_content_type(attachment) or attachment.content_type
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{content_type};base64,{_base64_payload(attachment.path)}"},
    }


def image_to_ollama_payload(image: dict[str, Any] | ImageAttachment) -> str:
    attachment = _as_attachment(image)
    if not is_image_attachment(attachment):
        raise ValueError("Only image attachments can be passed to vision models")
    return _base64_payload(attachment.path)


def attachment_to_dict(attachment: ImageAttachment) -> dict[str, Any]:
    return {
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "path": attachment.path,
        "size": attachment.size,
    }


def is_image_attachment(attachment: dict[str, Any] | ImageAttachment) -> bool:
    return infer_image_content_type(attachment) in SUPPORTED_IMAGE_TYPES


def infer_image_content_type(attachment: dict[str, Any] | ImageAttachment) -> str | None:
    normalized = _as_attachment(attachment)
    content_type = normalized.content_type.lower()
    if content_type in SUPPORTED_IMAGE_TYPES:
        return content_type
    return IMAGE_EXTENSIONS.get(Path(normalized.filename).suffix.lower())


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} МБ"
    if size >= 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size} Б"


def _is_allowed_attachment(size: int, max_bytes: int) -> bool:
    return 0 < size <= max_bytes


def _is_supported_image(content_type: str, size: int, max_bytes: int) -> bool:
    return content_type in SUPPORTED_IMAGE_TYPES and 0 < size <= max_bytes


def _write_image(directory: Path, filename: str, payload: bytes) -> Path:
    return _write_attachment(directory, filename, payload)


def _write_attachment(directory: Path, filename: str, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()[:16]
    path = directory / f"{digest}-{filename}"
    path.write_bytes(payload)
    return path


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9_.-]+", "_", filename).strip("._")
    return cleaned[:120] or "image"


def _extension(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type, mimetypes.guess_extension(content_type) or "")


def _base64_payload(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _as_attachment(image: dict[str, Any] | ImageAttachment) -> ImageAttachment:
    if isinstance(image, ImageAttachment):
        return image
    return ImageAttachment(
        filename=str(image.get("filename") or "image"),
        content_type=str(image.get("content_type") or "application/octet-stream"),
        path=str(image.get("path") or ""),
        size=int(image.get("size") or 0),
    )

