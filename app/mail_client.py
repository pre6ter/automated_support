import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from app.config import Config
from app.image_attachments import attachment_to_dict, save_email_images


@dataclass(frozen=True)
class IncomingEmail:
    mail_id: str
    sender: str
    recipients: str
    subject: str
    sent_at: str
    body: str
    attachments: list[dict[str, object]] = field(default_factory=list)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def fetch_recent_emails(config: Config) -> list[IncomingEmail]:
    if not config.mail_username or not config.mail_password:
        raise RuntimeError("MAIL_USERNAME and MAIL_PASSWORD must be set in .env")

    with imaplib.IMAP4_SSL(config.mail_host, config.mail_port) as client:
        client.login(config.mail_username, config.mail_password)
        status, _ = client.select(config.mail_folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Cannot open IMAP folder: {config.mail_folder}")

        status, payload = client.search(None, "ALL")
        if status != "OK" or not payload:
            return []

        message_numbers = payload[0].split()[-config.mail_fetch_limit :]
        emails: list[IncomingEmail] = []
        for number in reversed(message_numbers):
            status, data = client.fetch(number, "(RFC822)")
            if status != "OK":
                continue

            raw_message = _first_message_bytes(data)
            if not raw_message:
                continue

            parsed = message_from_bytes(raw_message)
            body = _extract_body(parsed).strip()
            if not body:
                body = "(Не удалось извлечь текст письма.)"

            sender = _decode_header_value(parsed.get("From", ""))
            recipients = _decode_recipients(parsed)
            if not is_message_eligible(config, {"sender": sender, "recipients": recipients}):
                continue

            subject = _decode_header_value(parsed.get("Subject", "(без темы)")) or "(без темы)"
            sent_at = _format_date(parsed.get("Date", ""))
            message_id = parsed.get("Message-ID", "")
            mail_id = _stable_mail_id(config.mail_username, message_id, sender, subject, sent_at)
            image_parts = _extract_image_parts(parsed)
            attachments = [
                attachment_to_dict(attachment)
                for attachment in save_email_images(
                    config.attachment_dir,
                    mail_id,
                    image_parts,
                    config.max_image_attachment_bytes,
                )
            ]

            emails.append(
                IncomingEmail(
                    mail_id=mail_id,
                    sender=sender,
                    recipients=recipients,
                    subject=subject,
                    sent_at=sent_at,
                    body=body[:20000],
                    attachments=attachments,
                )
            )

        return emails


def _first_message_bytes(data: list[bytes | tuple[bytes, bytes]]) -> bytes | None:
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return item[1]
    return None


def is_message_eligible(config: Config, message: dict[str, str]) -> bool:
    sender_addresses = _extract_addresses(message.get("sender", ""))
    recipient_addresses = _extract_addresses(message.get("recipients", ""))

    if config.owner_email and config.owner_email in sender_addresses:
        return False
    return bool(config.support_address and config.support_address in recipient_addresses)


def _decode_header_value(value: str) -> str:
    parts: list[str] = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            parts.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return "".join(parts).strip()


def _decode_recipients(message: Message) -> str:
    headers = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")
    values: list[str] = []
    for header in headers:
        values.extend(message.get_all(header, []))

    decoded_values = [_decode_header_value(value) for value in values]
    recipients = []
    for name, address in getaddresses(decoded_values):
        if address:
            label = f"{name} <{address}>" if name else address
            recipients.append(label)
    return ", ".join(recipients)


def _extract_addresses(value: str) -> set[str]:
    return {address.lower() for _, address in getaddresses([_decode_header_value(value)]) if address}


def _extract_body(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get("Content-Disposition", "").lower().startswith("attachment"):
                continue
            _append_decoded_part(part, plain_parts, html_parts)
    else:
        _append_decoded_part(message, plain_parts, html_parts)

    if plain_parts:
        return _clean_text("\n\n".join(plain_parts))
    return _clean_text("\n\n".join(_html_to_text(part) for part in html_parts))


def _extract_image_parts(message: Message) -> list[dict[str, object]]:
    parts = message.walk() if message.is_multipart() else [message]
    images: list[dict[str, object]] = []
    for part in parts:
        if part.get_content_maintype() != "image":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename()
        images.append(
            {
                "filename": _decode_header_value(filename) if filename else "",
                "content_type": part.get_content_type(),
                "payload": payload,
            }
        )
    return images


def _append_decoded_part(part: Message, plain_parts: list[str], html_parts: list[str]) -> None:
    content_type = part.get_content_type()
    if content_type not in {"text/plain", "text/html"}:
        return

    payload = part.get_payload(decode=True)
    if not payload:
        return

    charset = part.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if content_type == "text/plain":
        plain_parts.append(text)
    else:
        html_parts.append(text)


def _html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return extractor.text()


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _format_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def _stable_mail_id(account: str, message_id: str, sender: str, subject: str, sent_at: str) -> str:
    source = "|".join([account, message_id, sender, subject, sent_at])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
