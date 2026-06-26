from __future__ import annotations

import re


CORE_OFFER_NUMBER_TERMS = (
    "Номер предложения",
    "converter_id",
    "Converter.brandId",
    "Converter.number",
    "Converter",
    "brandId",
    "number",
    "purch_req_request",
    "production_order",
)

BUYERPRO_FLOW_TERMS = (
    "Список предложений",
    "Предложение (ТЭО)",
    "Согласование ТЭО",
    "converter",
    "acsapta_teo_approve",
    "AcsaptaTeoComment",
    "User",
    "user",
    "permission_rule",
    "user_direction",
    "ExDirection",
)


DOMAIN_DEFINITIONS = (
    "Номер предложения: два числа, разделённые точкой или запятой, например 12177.9 или 12177,9. "
    "В системе это соответствует паре полей Converter.brandId + Converter.number. "
    "В разных таблицах эта связь может называться converter_id или похожими комбинациями, "
    "например в purch_req_request или production_order.\n"
    "Раздел «Список предложений» во фронтенде соответствует converter flow: таблица Converter, "
    "API /buyerpro/converter/* и backend /api/v2/buyerpro/converter/*. Выгрузка в Аксапту и создание "
    "черновика ТЭО начинаются из этого раздела; важные поля Converter: status, teostatus, teoerror, "
    "ax_status, teoNum, exportId, userId, authorId, disabled.\n"
    "Раздел «ТЭО на согласовании» продолжает converter flow через Converter.exportId или converter_id "
    "в purch_req_request. Важные таблицы: purch_req_request для статуса created/wait/reviewed/"
    "rejected/approved/revoked, acsapta_teo_approve для согласования дирекций и AcsaptaTeoComment "
    "для комментариев. Если пользователь пишет про Аксапту, выгрузку, итоговый файл, кнопку отправки "
    "на согласование или статус ТЭО, сначала проверяй связку Converter -> purch_req_request -> "
    "acsapta_teo_approve.\n"
    "Пользователи и права согласования ТЭО: таблица public.\"User\" — пользователи приложения "
    "(email, fio, displayName, position, num, to, purch_req_allow), таблица public.\"user\" — синхронизированные "
    "пользователи buyernew (num, email, full_name, position, is_fired). Связка между ними обычно идёт по "
    "User.num -> user.num. purch_req_request хранит user_id/author_id из public.\"User\" и "
    "new_user_applicant_id/new_user_approver_id из public.\"user\".\n"
    "permission_rule определяет права согласования и автоматических получателей в копии письма при статусе "
    "«Согласовано»: type=user использует user_id из public.\"user\", type=position использует должность; "
    "approve_amount_from/to задаёт диапазон сумм для права согласования, mail_amount_from/to — диапазон сумм "
    "для добавления в копию письма. user_direction связывает пользователя из public.\"user\" с дирекцией, "
    "а ExDirection содержит список дирекций (key, title, tgs). Для проверки, почему пользователь может или "
    "не может согласовать ТЭО, проверяй общую дирекцию заявителя и согласующего через user_direction + "
    "ExDirection и подходящее правило permission_rule по сумме buyer_sum.\n"
    "Известная частая проблема: если у пользователей не загружаются, не скачиваются или не открываются "
    "файлы из BuyerPro размером больше 8 МБ, причина обычно в том, что Kaspersky блокирует загрузку "
    "таких файлов. В этом случае не нужно искать причину в SharePoint или converter: пользователю нужно "
    "ответить по шаблону, что это блокировка Kaspersky, и направить его в HelpDesk."
)


def domain_knowledge_prompt() -> str:
    return DOMAIN_DEFINITIONS


def offer_number_terms(text: str) -> list[str]:
    if not _has_offer_number_context(text):
        return []

    terms = list(CORE_OFFER_NUMBER_TERMS)
    terms.extend(_extract_offer_numbers(text))
    terms.extend(BUYERPRO_FLOW_TERMS)
    return terms


def parse_offer_numbers(text: str) -> list[tuple[int, int, str]]:
    values: list[tuple[int, int, str]] = []
    for raw_value in _extract_offer_numbers(text):
        left, right = re.split(r"[,.]", raw_value, maxsplit=1)
        values.append((int(left), int(right), raw_value))
    return values


def _has_offer_number_context(text: str) -> bool:
    lowered = text.lower()
    if "номер предложения" in lowered or "предложени" in lowered or "converter_id" in lowered:
        return True
    return bool(_extract_offer_numbers(text))


def _extract_offer_numbers(text: str) -> list[str]:
    return re.findall(r"\b\d{2,}[,.]\d+\b", text)

