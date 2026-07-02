import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.ai import (
    SupportResponse,
    _augment_chat_answer_with_excel_findings,
    _chat_messages,
    _clean_model_text,
    _diagnostic_context_prompt,
    _messages,
    _openai_messages,
    _parse_support_response,
    _prioritized_diagnostic_context,
    generate_chat_answer,
    generate_support_response,
)
from app.code_search_agent import (
    AGENT_SYSTEM_PROMPT,
    collect_agentic_code_context,
    execute_buyerpro_select,
    list_buyerpro_tables,
    parse_code_search_action,
    query_buyer_logs,
    read_repository_file,
    safe_repository_file_path,
    search_buyerpro_schema,
    should_run_agentic_search_for_chat,
    should_run_agentic_search_for_support,
)
from app.code_intelligence import collect_code_entity_context
from app.config import Config
from app.diagnostics import (
    _buyerpro_flow_queries,
    _chat_diagnostic_text,
    _compact_sql_result,
    _converter_problem_classification,
    _converter_upload_log_summary,
    _converter_upload_log_focus,
    _converter_upload_log_converter_ids,
    _entity_lookup_queries,
    _extract_lookup_identifiers,
    _offer_number_queries,
    _unique_offer_numbers,
)
from app.domain_knowledge import domain_knowledge_prompt
from app.domain_knowledge import parse_offer_numbers
from app.excel_inspector import _compare_template_header_columns, build_storage_url, parse_xlsx_xml
from app.image_attachments import infer_image_content_type, is_image_attachment
from app.mail_client import IncomingEmail, default_reply_recipients
from app.mcp_server import call_project_tool, handle_mcp_request
from app.mcp_client import load_mcp_server, validate_readonly_sql
from app.repository_context import collect_repository_context, extract_search_terms
from app.storage import get_generation_job, init_db, save_generation_job, upsert_message
from app.support_issue_parser import format_message_for_model, parse_support_issue_body
from app.taxonomy import ProblemCategory, guess_category, normalize_category


class TaxonomyTest(unittest.TestCase):
    def test_normalizes_aliases(self) -> None:
        self.assertEqual(normalize_category("Конвертер/Список предложений"), ProblemCategory.CONVERTER_OFFERS)
        self.assertEqual(normalize_category("Согласование ТЭО"), ProblemCategory.TEO_APPROVAL)
        self.assertEqual(normalize_category("buyer_pro"), ProblemCategory.CONVERTER_OFFERS)

    def test_guesses_category_from_text(self) -> None:
        category = guess_category("Ошибка BuyerPro", "Поставщик не может открыть pro.famil.ru")
        self.assertEqual(category, ProblemCategory.CONVERTER_OFFERS)


class AiParsingTest(unittest.TestCase):
    def test_parses_json_response(self) -> None:
        response = _parse_support_response(
            """
            ```json
            {
              "category": "teo_approval",
              "confidence": 0.81,
              "probable_problem": "Ошибка согласования ТЭО",
              "evidence": ["В письме указано согласование ТЭО"],
              "next_checks": ["Проверить логи"],
              "draft": "Здравствуйте! Проверим согласование."
            }
            ```
            """,
            _message(),
        )

        self.assertEqual(response.category, ProblemCategory.TEO_APPROVAL)
        self.assertEqual(response.confidence, 0.81)
        self.assertEqual(response.evidence, ["В письме указано согласование ТЭО"])
        self.assertEqual(response.draft, "Здравствуйте! Проверим согласование.")

    def test_falls_back_for_plain_text(self) -> None:
        response = _parse_support_response("Здравствуйте! Уточните номер заказа.", _message())
        self.assertEqual(response.category, ProblemCategory.CONVERTER_OFFERS)
        self.assertIn("Уточните номер заказа", response.draft)

    def test_clean_model_text_truncates_repetition_loop(self) -> None:
        repeated = (
            "Проверил контекст. Нужно уточнить номер предложения.\n\n"
            "Я вижу путь к файлу, но содержимое не загрузилось.\n\n"
            "Пожалуйста, пришлите номер поставщика.\n\n"
            "Пожалуйста, пришлите номер поставщика.\n\n"
            "Пожалуйста, пришлите номер поставщика.\n\n"
        )

        cleaned = _clean_model_text(repeated)

        self.assertIn("Проверил контекст", cleaned)
        self.assertEqual(cleaned.count("Пожалуйста, пришлите номер поставщика."), 1)

    def test_parsed_draft_truncates_repetition_loop(self) -> None:
        response = _parse_support_response(
            json.dumps(
                {
                    "category": "converter_offers",
                    "confidence": 0.7,
                    "probable_problem": "Нужно уточнение",
                    "evidence": [],
                    "next_checks": [],
                    "draft": (
                        "Добрый день!\n\n"
                        "Пожалуйста, пришлите номер поставщика.\n\n"
                        "Пожалуйста, пришлите номер поставщика.\n\n"
                        "Пожалуйста, пришлите номер поставщика."
                    ),
                },
                ensure_ascii=False,
            ),
            _message(),
        )

        self.assertEqual(response.draft.count("Пожалуйста, пришлите номер поставщика."), 1)

    def test_chat_messages_include_history_and_question(self) -> None:
        messages = _chat_messages(
            [{"role": "user", "content": "Первый вопрос"}],
            "Второй вопрос",
            {"dbhub": {"database": "buyerpro"}, "grafana": {"summary": "Starting batch processing: 0 items"}},
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Номер предложения", messages[0]["content"])
        self.assertIn("Converter.brandId", messages[0]["content"])
        self.assertIn("Диагностический контекст", messages[0]["content"])
        self.assertIn("buyerpro", messages[0]["content"])
        self.assertIn("excel_file_xml_inspection", messages[0]["content"])
        self.assertIn("Starting batch processing: 0 items", messages[0]["content"])
        self.assertIn("Не спрашивай разрешение выполнить поиск", messages[0]["content"])
        self.assertEqual(messages[-2]["content"], "Первый вопрос")
        self.assertEqual(messages[-1]["content"], "Второй вопрос")

    def test_chat_answer_appends_excel_template_column_findings(self) -> None:
        answer = _augment_chat_answer_with_excel_findings(
            "В файле найдены критичные ошибки структуры.",
            {
                "dbhub": {
                    "buyerpro_flow_lookup": [
                        {
                            "query": "excel_file_xml_inspection",
                            "result": {
                                "download_teo_checks": {
                                    "checks": [
                                        {
                                            "name": "source_template_reference_columns",
                                            "status": "failed",
                                            "details": {
                                                "missing_columns": [
                                                    {"column": "AF", "expected": "F*РРЦ"},
                                                ],
                                                "mismatched_columns": [
                                                    {
                                                        "column": "B",
                                                        "expected": "V*Brand",
                                                        "actual": "Brand changed",
                                                    },
                                                ],
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            },
        )

        self.assertIn("пропущена/удалена колонка AF: `F*РРЦ`", answer)
        self.assertIn("отличается название колонки B: ожидалось `V*Brand`, в файле `Brand changed`", answer)

    def test_chat_answer_appends_excel_quantity_column_findings(self) -> None:
        answer = _augment_chat_answer_with_excel_findings(
            "В файле найдены строки, которые не попадут в ТЭО.",
            {
                "dbhub": {
                    "buyerpro_flow_lookup": [
                        {
                            "query": "excel_file_xml_inspection",
                            "result": {
                                "download_teo_checks": {
                                    "checks": [
                                        {
                                            "name": "source_template_item_rows",
                                            "status": "failed",
                                            "details": {
                                                "rows_with_model": 10,
                                                "skipped_by_empty_or_zero_quantity": 10,
                                                "quantity_issues": [
                                                    {
                                                        "quantity_column": "AC",
                                                        "quantity_header": "F*Заказ шт",
                                                        "neighbor_quantity_column": "AB",
                                                        "neighbor_quantity_header": "V*Количество",
                                                        "neighbor_quantity_value": "2",
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            },
        )

        self.assertIn("колонка AC `F*Заказ шт` пустая или 0", answer)
        self.assertIn("в AB `V*Количество` стоит `2`", answer)

    def test_chat_diagnostic_text_preserves_follow_up_context(self) -> None:
        diagnostic_text = _chat_diagnostic_text(
            "Ты можешь скачать файл предложения Converter.localFile и посмотреть?",
            [
                {"role": "user", "content": "8096.11"},
                {"role": "assistant", "content": "Нашёл предложение 8096.11."},
            ],
        )

        self.assertIn("8096.11", diagnostic_text)
        self.assertIn("Converter.localFile", diagnostic_text)
        self.assertIn("Текущий вопрос пользователя", diagnostic_text)

    def test_offline_chat_answer(self) -> None:
        answer, provider, model = generate_chat_answer(_config(()), [], "Что умеет приложение?")

        self.assertEqual(provider, "offline")
        self.assertEqual(model, "template")
        self.assertIn("Что умеет приложение?", answer)

    def test_openai_messages_include_image_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "screen.png"
            image_path.write_bytes(b"fake-png")
            messages = _openai_messages(
                [{"role": "user", "content": "Что на скриншоте?"}],
                [{"filename": "screen.png", "content_type": "image/png", "path": str(image_path), "size": 8}],
            )

        self.assertIsInstance(messages[-1]["content"], list)
        self.assertEqual(messages[-1]["content"][0]["type"], "text")
        self.assertEqual(messages[-1]["content"][1]["type"], "image_url")

    def test_openai_messages_include_image_with_wrong_email_mime_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "screen.jpg"
            image_path.write_bytes(b"fake-jpeg")
            attachment = {
                "filename": "screen.jpg",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "path": str(image_path),
                "size": 9,
            }
            messages = _openai_messages([{"role": "user", "content": "Что на скриншоте?"}], [attachment])

        image_part = messages[-1]["content"][1]
        self.assertTrue(is_image_attachment(attachment))
        self.assertEqual(infer_image_content_type(attachment), "image/jpeg")
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_prioritized_context_puts_dbhub_facts_first(self) -> None:
        context = _prioritized_diagnostic_context(
            {
                "code": {"user_summary": "много кода"},
                "dbhub": {"database": "buyerpro", "entity_data_lookup": [{"result": "status done"}]},
                "repository": {"matches": [{"text": "x"} for _ in range(20)]},
            }
        )

        self.assertIn("dbhub_facts_first", list(context.keys())[0])
        self.assertEqual(context["dbhub_facts_first"]["entity_data_lookup"][0]["result"], "status done")
        self.assertNotIn("sources", context)
        self.assertNotIn("grafana_logs", context)
        self.assertNotIn("db_terms", context["code_entity_understanding"])
        self.assertNotIn("derived_terms", context["code_entity_understanding"])

    def test_prioritized_context_drops_empty_lookup_items(self) -> None:
        context = _prioritized_diagnostic_context(
            {
                "dbhub": {
                    "offer_number_lookup": [
                        {"query": "Converter", "count": 0, "summary": "Записи не найдены."},
                        {"query": "purch_req_request", "count": 1, "rows": [{"id": 1}]},
                    ],
                    "entity_data_lookup": [
                        {"entity": "sku", "query": "sku", "result": "Записи не найдены."},
                        {"entity": "sku", "query": "sku", "result": '{"rows":[{"id":1}]}'},
                    ],
                },
                "code": {},
                "repository": {},
            }
        )

        self.assertEqual(len(context["dbhub_facts_first"]["offer_number_lookup"]), 1)
        self.assertEqual(context["dbhub_facts_first"]["offer_number_lookup"][0]["query"], "purch_req_request")
        self.assertEqual(len(context["dbhub_facts_first"]["entity_data_lookup"]), 1)

    def test_excel_inspection_stays_in_prompt_before_large_flow_lookup(self) -> None:
        diagnostic_prompt = _diagnostic_context_prompt(
            {
                "dbhub": {
                    "database": "buyerpro",
                    "buyerpro_flow_lookup": [
                        {"query": "converter_status_for_offer_list", "result": "x" * 20000},
                        {
                            "query": "excel_file_xml_inspection",
                            "result": {
                                "summary": "Excel-файл распаршен как XLSX/XML",
                                "interesting_values": [
                                    {"sheet": "Вводные", "cell": "B1", "label": "Поставщик", "value_right": "П07655"}
                                ],
                                "download_teo_checks": {"summary": "download/teo checks ok"},
                            },
                        },
                    ],
                },
                "code": {},
                "repository": {},
                "grafana": {},
            }
        )

        self.assertIn("excel_file_xml_inspection", diagnostic_prompt)
        self.assertIn("П07655", diagnostic_prompt)
        self.assertIn("результат сокращён", diagnostic_prompt)

    def test_upload_log_summary_stays_before_large_dbhub_context(self) -> None:
        diagnostic_prompt = _diagnostic_context_prompt(
            {
                "dbhub": {
                    "database": "buyerpro",
                    "buyerpro_flow_lookup": [
                        {
                            "query": "converter_problem_classification",
                            "problem_key": "converter_upload",
                            "problem_label": "Проблема при загрузке в конвертер",
                        }
                    ],
                    "summary": [{"term": f"term-{index}", "result": "x" * 1000} for index in range(30)],
                },
                "grafana": {
                    "logql": "{container=~\"buyerproworker0|buyerproworker1\"}",
                    "summary": "Диагностический вывод: Starting batch processing: 0 items",
                    "log_focus": {"converter_ids": ["75790"]},
                },
                "code": {},
                "repository": {},
            }
        )

        self.assertIn("Starting batch processing: 0 items", diagnostic_prompt)
        self.assertIn("converter_upload_logs", diagnostic_prompt)
        self.assertNotIn("schema_search_summary", diagnostic_prompt)
        self.assertNotIn("term-29", diagnostic_prompt)

    def test_agentic_sql_rows_stay_visible_in_prompt(self) -> None:
        diagnostic_prompt = _diagnostic_context_prompt(
            {
                "dbhub": {"summary": [{"term": f"term-{index}", "result": "x" * 1000} for index in range(30)]},
                "code": {},
                "repository": {},
                "agentic_code_search": {
                    "enabled": True,
                    "summary": "Agentic diagnostics выполнил SQL.",
                    "steps": [
                        {
                            "step": 7,
                            "action": "execute_sql",
                            "sql": (
                                "SELECT period_start_date, period_end_date FROM category_prohibited_periods "
                                "WHERE category_id = '1021.1.4' AND season_id = 6"
                            ),
                            "result": {
                                "ok": True,
                                "database": "buyerpro",
                                "result": json.dumps(
                                    {
                                        "success": True,
                                        "data": {
                                            "rows": [
                                                {
                                                    "period_start_date": "2026-08-13T00:00:00.000Z",
                                                    "period_end_date": "2027-03-01T00:00:00.000Z",
                                                }
                                            ],
                                            "count": 1,
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
            }
        )

        self.assertIn("important_results", diagnostic_prompt)
        self.assertIn("1021.1.4", diagnostic_prompt)
        self.assertIn("2026-08-13T00:00:00.000Z", diagnostic_prompt)
        self.assertIn("2027-03-01T00:00:00.000Z", diagnostic_prompt)

    def test_compact_buyerpro_flow_drops_empty_rows(self) -> None:
        compact = _prioritized_diagnostic_context(
            {
                "dbhub": {
                    "buyerpro_flow_lookup": [
                        {"query": "teo_request_for_approval", "rows": [], "count": 0, "summary": "Записи не найдены."}
                    ]
                },
                "grafana": {},
                "code": {},
                "repository": {},
            }
        )["dbhub_facts_first"]["buyerpro_flow_lookup"]

        self.assertEqual(compact, [{"query": "teo_request_for_approval", "count": 0, "summary": "Записи не найдены."}])

    def test_compact_buyerpro_flow_skips_teo_queries_for_upload_problem(self) -> None:
        compact = _prioritized_diagnostic_context(
            {
                "dbhub": {
                    "buyerpro_flow_lookup": [
                        {"query": "converter_problem_classification", "problem_key": "converter_upload"},
                        {"query": "converter_status_for_offer_list", "rows": [{"id": "75790"}], "count": 1},
                        {"query": "teo_request_for_approval", "count": 0, "summary": "Записи не найдены."},
                    ]
                },
                "grafana": {},
                "code": {},
                "repository": {},
            }
        )["dbhub_facts_first"]["buyerpro_flow_lookup"]

        self.assertEqual([item["query"] for item in compact], ["converter_problem_classification", "converter_status_for_offer_list"])

    def test_known_kaspersky_file_issue_uses_template_for_email(self) -> None:
        message = {
            **_message(),
            "subject": "Не выгружается файл из BuyerPro",
            "body": "Не получается скачать файл 12 МБ из БайерПро.",
        }

        response, provider, model = generate_support_response(_config(()), message)

        self.assertEqual(provider, "rule")
        self.assertEqual(model, "known-issue")
        self.assertEqual(response.category, ProblemCategory.CONVERTER_OFFERS)
        self.assertIn("Kaspersky блокирует загрузку файлов из BuyerPro", response.draft)
        self.assertIn("HelpDesk", response.draft)

    def test_known_kaspersky_file_issue_uses_template_for_chat(self) -> None:
        answer, provider, model = generate_chat_answer(
            _config(()),
            [],
            "У пользователя Kaspersky блокирует файл из БП, не скачивается выгрузка.",
        )

        self.assertEqual(provider, "rule")
        self.assertEqual(model, "known-issue")
        self.assertIn("HelpDesk", answer)

    def test_agentic_chat_fallback_retries_with_expanded_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()
            config = replace(_config((repo,)), ai_provider="lmstudio", lm_studio_model="local")
            diagnostic_context = {"repository": {"matches": []}, "code": {}, "sources": []}

            with patch(
                "app.ai._generate_lm_studio_chat_answer",
                side_effect=[
                    "Не хватает контекста по коду.",
                    '{"action":"finish","summary":"Нужно смотреть buyerproback/upload.service.ts"}',
                    "Финальный ответ по расширенному контексту.",
                ],
            ) as mocked_model:
                answer, provider, model = generate_chat_answer(config, [], "Где обрабатывается upload/normal?", [], diagnostic_context)

        self.assertEqual(provider, "lmstudio")
        self.assertEqual(model, "local")
        self.assertEqual(answer, "Финальный ответ по расширенному контексту.")
        self.assertEqual(mocked_model.call_count, 3)
        self.assertIn("agentic_code_search", diagnostic_context)
        self.assertEqual(diagnostic_context["agentic_code_search"]["summary"], "Нужно смотреть buyerproback/upload.service.ts")

    def test_agentic_support_fallback_retries_low_confidence_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()
            config = replace(_config((repo,)), ai_provider="lmstudio", lm_studio_model="local")
            diagnostic_context = {"repository": {"matches": [{"path": "x.ts"}]}, "code": {}, "sources": []}

            with (
                patch(
                    "app.ai._generate_lm_studio_response",
                    side_effect=[
                        SupportResponse(
                            category=ProblemCategory.CONVERTER_OFFERS,
                            confidence=0.3,
                            probable_problem="Данных недостаточно",
                            next_checks=["Нужно уточнить"],
                            draft="Добрый день! Нужно уточнить детали.",
                        ),
                        SupportResponse(
                            category=ProblemCategory.CONVERTER_OFFERS,
                            confidence=0.82,
                            probable_problem="Ошибка в обработке upload/normal",
                            evidence=["Найден дополнительный кодовый контекст"],
                            draft="Добрый день! Ошибка связана с обработкой upload/normal.",
                        ),
                    ],
                ) as mocked_response,
                patch(
                    "app.ai._generate_lm_studio_chat_answer",
                    return_value='{"action":"finish","summary":"Нашёл дополнительный кодовый контекст"}',
                ) as mocked_agent,
            ):
                response, provider, model = generate_support_response(config, _message(), diagnostic_context)

        self.assertEqual(provider, "lmstudio")
        self.assertEqual(model, "local")
        self.assertGreaterEqual(response.confidence, 0.82)
        self.assertIn("upload/normal", response.draft)
        self.assertEqual(mocked_response.call_count, 2)
        self.assertEqual(mocked_agent.call_count, 1)
        self.assertIn("agentic_code_search", diagnostic_context)

    def test_agentic_support_runs_for_confident_non_hardcoded_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()
            config = replace(_config((repo,)), ai_provider="lmstudio", lm_studio_model="local")
            diagnostic_context = {"repository": {"matches": [{"path": "x.ts"}]}, "code": {}, "sources": []}

            with (
                patch(
                    "app.ai._generate_lm_studio_response",
                    side_effect=[
                        SupportResponse(
                            category=ProblemCategory.CONVERTER_OFFERS,
                            confidence=0.9,
                            probable_problem="Ошибка выгрузки ТЭО",
                            draft="Добрый день! В файле есть ошибка.",
                        ),
                        SupportResponse(
                            category=ProblemCategory.CONVERTER_OFFERS,
                            confidence=0.9,
                            probable_problem="Ошибка выгрузки ТЭО с кодовым контекстом",
                            draft="Добрый день! Проверили условия в коде.",
                        ),
                    ],
                ) as mocked_response,
                patch(
                    "app.ai._generate_lm_studio_chat_answer",
                    return_value='{"action":"finish","summary":"Кодовый контекст нужен для не hardcoded случая"}',
                ) as mocked_agent,
            ):
                response, provider, model = generate_support_response(config, _message(), diagnostic_context)

        self.assertEqual(provider, "lmstudio")
        self.assertEqual(model, "local")
        self.assertIn("коде", response.draft.lower())
        self.assertEqual(mocked_response.call_count, 2)
        self.assertEqual(mocked_agent.call_count, 1)

    def test_agentic_search_skips_hardcoded_upload_and_column_cases(self) -> None:
        response = SupportResponse(
            category=ProblemCategory.CONVERTER_OFFERS,
            confidence=0.2,
            probable_problem="Нужно уточнить",
            draft="Добрый день!",
        )
        upload_context = {
            "dbhub": {
                "buyerpro_flow_lookup": [
                    {"query": "converter_problem_classification", "problem_key": "converter_upload"}
                ]
            }
        }
        column_context = {
            "dbhub": {
                "buyerpro_flow_lookup": [
                    {
                        "query": "excel_file_xml_inspection",
                        "result": {
                            "download_teo_checks": {
                                "checks": [
                                    {"name": "source_template_reference_columns", "status": "failed"}
                                ]
                            }
                        },
                    }
                ]
            }
        }

        self.assertFalse(should_run_agentic_search_for_chat("Не хватает контекста", upload_context))
        self.assertFalse(should_run_agentic_search_for_support(response, upload_context, min_confidence=0.65))
        self.assertFalse(should_run_agentic_search_for_chat("Не хватает контекста", column_context))
        self.assertFalse(should_run_agentic_search_for_support(response, column_context, min_confidence=0.65))
        self.assertTrue(should_run_agentic_search_for_chat("Уверенный ответ", {"repository": {"matches": [{"path": "x"}]}}))


class SupportIssueParserTest(unittest.TestCase):
    def test_parses_buyerpro_feedback_form(self) -> None:
        parsed = parse_support_issue_body(_buyerpro_feedback_body())

        self.assertEqual(parsed["title"], "Другое - Вопрос по работе с системой")
        self.assertEqual(parsed["created_at"], "30.06.2026 11:55:36")
        self.assertEqual(parsed["user"], "mironov.nikolay@FAMIL.RU")
        self.assertEqual(parsed["uid"], "f72fbbca-e78a-429c-88d7-023085a484ca")
        self.assertEqual(parsed["ticket_number"], "546")
        self.assertEqual(parsed["offer_number"], "23")
        self.assertEqual(parsed["problem_description"], "Просто тест обратной связи после хотфикса")

    def test_formats_structured_message_for_model(self) -> None:
        message = {**_message(), "body": _buyerpro_feedback_body()}
        formatted = format_message_for_model(message)
        prompt = _messages(message, None)[1]["content"]

        self.assertIn("Структурированный разбор обращения", formatted)
        self.assertIn("- Номер предложения: 23", prompt)
        self.assertIn("- Описание проблемы: Просто тест обратной связи после хотфикса", prompt)
        self.assertIn("Исходный текст письма", prompt)


class MailRecipientsTest(unittest.TestCase):
    def test_default_reply_recipients_matches_reply_all(self) -> None:
        recipients = default_reply_recipients(
            "Иван <ivan@example.com>",
            "buyerpro-support@famil.ru, Мария <maria@example.com>, ivan@example.com",
            {"buyerpro-support@famil.ru"},
        )

        self.assertEqual(recipients, "Иван <ivan@example.com>, Мария <maria@example.com>")


class SqlValidatorTest(unittest.TestCase):
    def test_allows_select(self) -> None:
        self.assertEqual(validate_readonly_sql("select * from public.orders limit 1;"), "select * from public.orders limit 1")

    def test_rejects_mutation(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_sql("update public.orders set status = 'x'")

    def test_rejects_multiple_statements(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_sql("select 1; select 2")

    def test_loads_direct_mcp_server_without_cursor_config(self) -> None:
        server = load_mcp_server(
            Path("/missing"),
            "grafana-pro",
            direct_url="http://mcp.example.com",
            direct_headers={"Authorization": "Bearer token"},
        )

        self.assertIsNotNone(server)
        self.assertEqual(server.url, "http://mcp.example.com")
        self.assertEqual(server.headers["Authorization"], "Bearer token")


class McpServerTest(unittest.TestCase):
    def test_initializes_and_lists_tools(self) -> None:
        config = _config(())

        initialize = handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, config)
        tools = handle_mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, config)

        self.assertEqual(initialize["result"]["serverInfo"]["name"], "automated_support")
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("collect_chat_diagnostics", tool_names)
        self.assertIn("get_message_context", tool_names)
        self.assertIn("execute_dbhub_select", tool_names)

    def test_calls_classification_tool(self) -> None:
        config = _config(())

        response = handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "classify_support_issue",
                    "arguments": {"subject": "Ошибка BuyerPro", "body": "Поставщик не может открыть pro.famil.ru"},
                },
            },
            config,
        )

        self.assertFalse(response["result"]["isError"])
        payload = response["result"]["content"][0]["text"]
        self.assertIn('"category": "converter_offers"', payload)

    def test_reads_saved_message_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(_config(()), database_path=Path(directory) / "support.sqlite3")
            init_db(config.database_path)
            upsert_message(
                config.database_path,
                IncomingEmail(
                    mail_id="mail-1",
                    sender="user@example.com",
                    recipients="buyerpro-support@famil.ru",
                    subject="Ошибка BuyerPro",
                    sent_at="2026-06-30",
                    body="Не открывается кабинет поставщика.",
                ),
            )

            context = call_project_tool("get_message_context", {"mail_id": "mail-1"}, config)

        self.assertEqual(context["mail_id"], "mail-1")
        self.assertEqual(context["subject"], "Ошибка BuyerPro")
        self.assertEqual(context["attachments"], [])

    def test_dbhub_select_tool_reports_readonly_errors(self) -> None:
        config = _config(())

        response = handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "execute_dbhub_select", "arguments": {"sql": "update public.orders set status = 'x'"}},
            },
            config,
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("Only read-only SQL statements are allowed", response["result"]["content"][0]["text"])


class DomainKnowledgeTest(unittest.TestCase):
    def test_domain_prompt_contains_offer_number_mapping(self) -> None:
        prompt = domain_knowledge_prompt()

        self.assertIn("Номер предложения", prompt)
        self.assertIn("Converter.brandId", prompt)
        self.assertIn("converter_id", prompt)
        self.assertIn("Список предложений", prompt)
        self.assertIn("ТЭО на согласовании", prompt)
        self.assertIn("Kaspersky", prompt)
        self.assertIn("permission_rule", prompt)
        self.assertIn("user_direction", prompt)
        self.assertIn("ExDirection", prompt)
        self.assertIn("User.num -> user.num", prompt)
        self.assertNotIn("Товарная группа, категория, ТГ", prompt)
        self.assertNotIn("category_id", prompt)

    def test_offer_number_expands_repository_terms(self) -> None:
        message = {
            "subject": "Номер предложения 12177.9",
            "body": "Проверьте номер предложения 12177,9 в заявке.",
        }

        terms = extract_search_terms(message, ProblemCategory.CONVERTER_OFFERS)

        self.assertIn("12177.9", terms)
        self.assertIn("12177,9", terms)
        self.assertIn("converter_id", terms)
        self.assertIn("purch_req_request", terms)
        self.assertIn("production_order", terms)

    def test_parse_offer_numbers(self) -> None:
        self.assertEqual(
            parse_offer_numbers("номер предложения 24392.4 и номер предложения 12177,9"),
            [(24392, 4, "24392.4"), (12177, 9, "12177,9")],
        )
        self.assertEqual(parse_offer_numbers("статус 24392.4 и 12177,9"), [])

    def test_parse_offer_numbers_ignores_product_group_and_season_values(self) -> None:
        text = "Ошибка как на скриншоте для товарной группы 1021,1,4 для 6го сезона. Какие разрешены даты выдачи?"

        self.assertEqual(parse_offer_numbers(text), [])
        self.assertNotIn("converter_id", extract_search_terms({"subject": text, "body": ""}, ProblemCategory.CONVERTER_OFFERS))

    def test_parse_offer_numbers_ignores_product_group_in_offer_teo_form(self) -> None:
        text = (
            "Предложение (ТЭО) - Ошибка в работе системы\n"
            "Ошибка как на скриншоте для товарной группы 1021,1,4 для 6го сезона."
        )

        self.assertEqual(parse_offer_numbers(text), [])

    def test_parse_offer_numbers_keeps_explicit_offer_context(self) -> None:
        text = "Проверьте номер предложения 3786.117: ошибка для товарной группы 1021,1,4."

        self.assertEqual(parse_offer_numbers(text), [(3786, 117, "3786.117")])

    def test_unique_offer_numbers_deduplicates_repeated_context(self) -> None:
        self.assertEqual(
            _unique_offer_numbers("номер предложения 56.158 повтор номер предложения 56,158 и еще converter 24392.4"),
            [(56, 158, "56.158"), (24392, 4, "24392.4")],
        )

    def test_compact_sql_result_removes_mcp_wrapper(self) -> None:
        compact = _compact_sql_result(
            {
                "success": True,
                "data": {
                    "rows": [{"id": "75790", "localFile": None}],
                    "count": 1,
                    "source_id": "buyerpro",
                },
            }
        )

        self.assertEqual(compact["count"], 1)
        self.assertEqual(compact["rows"], [{"id": "75790", "localFile": None}])
        self.assertNotIn("success", compact)
        self.assertNotIn("data", compact)

    def test_offer_number_queries_are_buyerpro_readonly(self) -> None:
        queries = _offer_number_queries(24392, 4)

        self.assertEqual([label for label, _ in queries], ["Converter", "purch_req_request", "production_order"])
        for _, sql in queries:
            self.assertIn("public.", sql)
            self.assertIn("24392", sql)
            self.assertIn("4", sql)
            self.assertTrue(validate_readonly_sql(sql).lower().startswith("select"))

    def test_buyerpro_flow_queries_are_buyerpro_readonly(self) -> None:
        queries = _buyerpro_flow_queries(24392, 4)

        self.assertEqual(
            [label for label, _ in queries],
            [
                "converter_status_for_offer_list",
                "teo_request_for_approval",
                "teo_direction_approvals",
                "teo_recent_activity",
                "teo_user_identity",
                "teo_user_directions",
                "teo_permission_rules_for_amount",
                "exdirection_reference",
            ],
        )
        joined_sql = "\n".join(sql for _, sql in queries)
        self.assertIn("public.\"Converter\"", joined_sql)
        self.assertIn("public.purch_req_request", joined_sql)
        self.assertIn("public.acsapta_teo_approve", joined_sql)
        self.assertIn("public.\"AcsaptaTeoComment\"", joined_sql)
        self.assertIn("public.\"User\"", joined_sql)
        self.assertIn("public.\"user\"", joined_sql)
        self.assertIn("public.permission_rule", joined_sql)
        self.assertIn("public.user_direction", joined_sql)
        self.assertIn("public.\"ExDirection\"", joined_sql)
        self.assertIn("c.\"localFile\"", joined_sql)
        self.assertIn("pr.local_file", joined_sql)
        for _, sql in queries:
            validated = validate_readonly_sql(sql).lower()
            self.assertTrue(validated.startswith(("with", "select")))

    def test_converter_problem_detects_missing_local_file(self) -> None:
        problem = _converter_problem_classification(
            "Проверь номер предложения 24392.4",
            [
                {
                    "id": 987,
                    "localFile": "",
                    "createdAt": "2026-06-30T09:31:06.109Z",
                    "updatedAt": "2026-06-30T09:35:00.000Z",
                }
            ],
            [],
        )

        self.assertEqual(problem["problem_key"], "converter_upload")
        self.assertEqual(problem["problem_label"], "Проблема при загрузке в конвертер")
        self.assertIn("987", problem["converter_ids"])
        self.assertIn("2026-06-30T09:31:06.109Z", problem["created_at_values"])
        self.assertIn("2026-06-30T09:35:00.000Z", problem["updated_at_values"])
        self.assertEqual(
            _converter_upload_log_converter_ids({"buyerpro_flow_lookup": [problem]}),
            ["987"],
        )
        focus = _converter_upload_log_focus({"buyerpro_flow_lookup": [problem]})
        self.assertEqual(focus["start"].isoformat(), "2026-06-30T09:30:06.109000+00:00")
        self.assertEqual(focus["end"].isoformat(), "2026-06-30T09:37:00+00:00")

    def test_converter_upload_log_summary_starts_from_upload_marker(self) -> None:
        summary = _converter_upload_log_summary(
            {
                "data": [
                    {
                        "timestamp": '"100"',
                        "line": "ERROR [converterHelper]: чужая ошибка до старта",
                        "labels": {"container": "buyerproworker0", "stream": "stderr"},
                    },
                    {
                        "timestamp": '"200"',
                        "line": "INFO [WORKER]: Запускаю загрузку предложения 987 в конвертер...",
                        "labels": {"container": "buyerproworker0", "stream": "stdout"},
                    },
                    {
                        "timestamp": '"300"',
                        "line": "INFO [WORKER]: Starting batch processing: 0 items, 0 batches (batch size: 1000)",
                        "labels": {"container": "buyerproworker0", "stream": "stdout"},
                    },
                    {
                        "timestamp": '"400"',
                        "line": "ERROR [converterHelper]: ошибка после старта",
                        "labels": {"container": "buyerproworker0", "stream": "stderr"},
                    },
                ]
            },
            ["987"],
            limit=1000,
        )

        self.assertNotIn("чужая ошибка до старта", summary)
        self.assertIn("воркер прочитал XLSX, но не нашёл ни одной валидной строки", summary)
        self.assertIn("наименования обязательных колонок", summary)
        self.assertIn("Запускаю загрузку предложения 987", summary)
        self.assertIn("Starting batch processing: 0 items", summary)
        self.assertIn("ошибка после старта", summary)

    def test_converter_problem_detects_exportteo_text(self) -> None:
        problem = _converter_problem_classification(
            "Ошибка при выгрузке в Аксапту через exportteo",
            [{"id": 987, "localFile": "storage/file.xlsx"}],
            [],
        )

        self.assertEqual(problem["problem_key"], "converter_export_teo")
        self.assertEqual(problem["problem_label"], "Проблема с выгрузкой в аксапту")

    def test_generic_entity_lookup_queries_are_readonly(self) -> None:
        identifiers = _extract_lookup_identifiers("статус заказа PO-123 и заявки 456")
        queries = [
            *_entity_lookup_queries("purchase_request", identifiers),
            *_entity_lookup_queries("production_order", identifiers),
        ]

        self.assertTrue(queries)
        for _, sql in queries:
            self.assertTrue(validate_readonly_sql(sql).lower().startswith("select"))
            self.assertIn("public.", sql)


class RepositoryContextTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("rg") is None, "ripgrep is not installed")
    def test_collects_matches_from_allowlisted_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "buyerproback"
            repo.mkdir()
            (repo / "handler.ts").write_text("throw new Error('BuyerPro API timeout')\n", encoding="utf-8")
            config = _config((repo,))

            context = collect_repository_context(config, _message(), ProblemCategory.CONVERTER_OFFERS)

            self.assertTrue(context["matches"])
            self.assertEqual(context["matches"][0]["repository"], "buyerproback")

    @unittest.skipIf(shutil.which("rg") is None, "ripgrep is not installed")
    def test_code_entity_context_uses_frontend_then_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            front = root / "buyerprofront"
            back = root / "buyerproback"
            front.mkdir()
            back.mkdir()
            (front / "Offer.tsx").write_text("const label = 'Номер предложения'; const field = 'converter_id';\n", encoding="utf-8")
            (back / "offer.service.ts").write_text("return db.production_order.find({ converter_id });\n", encoding="utf-8")
            config = _config((front, back))

            context = collect_code_entity_context(
                config,
                {"subject": "Статус номера предложения 24392.4", "body": "Что сейчас с предложением?"},
                ProblemCategory.CONVERTER_OFFERS,
            )

            self.assertEqual(context["flow"], "frontend_code -> backend_code -> db/logs")
            self.assertTrue(context["frontend"]["matches"])
            self.assertTrue(context["backend"]["matches"])
            self.assertEqual(context["entities"][0]["key"], "offer")
            self.assertIn("Converter", context["db_terms"])

    def test_agentic_action_parser_accepts_json_fence(self) -> None:
        action = parse_code_search_action(
            """
            ```json
            {"action":"search","query":"upload/normal","reason":"найти endpoint"}
            ```
            """
        )

        self.assertEqual(action.action, "search")
        self.assertEqual(action.query, "upload/normal")

    def test_agent_prompt_does_not_ask_permission_for_tools(self) -> None:
        self.assertIn("Не спрашивай пользователя", AGENT_SYSTEM_PROMPT)
        self.assertIn("сразу выбери соответствующее действие", AGENT_SYSTEM_PROMPT)

    def test_agentic_action_parser_accepts_db_and_log_tools(self) -> None:
        for action_name in ("list_tables", "search_schema", "execute_sql", "query_logs"):
            action = parse_code_search_action(json.dumps({"action": action_name, "query": "select 1"}))
            self.assertEqual(action.action, action_name)

    def test_agentic_action_parser_preserves_long_sql_query(self) -> None:
        sql = "select " + ", ".join(f"column_{index}" for index in range(120)) + " from public.table_name limit 1"

        action = parse_code_search_action(json.dumps({"action": "execute_sql", "query": sql}))

        self.assertEqual(action.query, sql)

    def test_safe_repository_file_path_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()

            with self.assertRaises(ValueError):
                safe_repository_file_path(repo, "../secret.txt")

    def test_agentic_read_file_stays_inside_allowlisted_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()
            (repo / "handler.ts").write_text("export const endpoint = 'upload/normal';\n", encoding="utf-8")

            result = read_repository_file((repo,), "buyerproback", "handler.ts", max_lines=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["repository"], "buyerproback")
        self.assertIn("upload/normal", result["content"])

    def test_agentic_list_tables_uses_buyerpro_schema_search(self) -> None:
        fake_client = _FakeMcpClient({"content": [{"text": '{"data":{"rows":[{"table":"Converter"}]}}'}]})

        with patch("app.code_search_agent._dbhub_client", return_value=fake_client):
            result = list_buyerpro_tables(_config(()), "Converter")

        self.assertTrue(result["ok"])
        self.assertEqual(result["database"], "buyerpro")
        self.assertEqual(fake_client.calls[0][0], "search_objects_buyerpro")
        self.assertEqual(fake_client.calls[0][1]["object_type"], "table")
        self.assertEqual(fake_client.calls[0][1]["pattern"], "%Converter%")

    def test_agentic_search_schema_checks_tables_and_columns(self) -> None:
        fake_client = _FakeMcpClient({"content": [{"text": '{"success":true,"data":{"results":[]}}'}]})

        with patch("app.code_search_agent._dbhub_client", return_value=fake_client):
            result = search_buyerpro_schema(_config(()), "season")

        self.assertTrue(result["ok"])
        self.assertEqual(result["database"], "buyerpro")
        self.assertEqual([call[1]["object_type"] for call in fake_client.calls], ["table", "column"])
        self.assertEqual(fake_client.calls[0][1]["pattern"], "%season%")

    def test_agentic_execute_sql_allows_only_readonly_buyerpro_select(self) -> None:
        fake_client = _FakeMcpClient({"content": [{"text": '{"rows":[{"id":1}]}'}]})

        with patch("app.code_search_agent._dbhub_client", return_value=fake_client):
            result = execute_buyerpro_select(_config(()), "select * from public.\"Converter\" limit 1;")
            rejected = execute_buyerpro_select(_config(()), "delete from public.\"Converter\"")

        self.assertTrue(result["ok"])
        self.assertEqual(result["database"], "buyerpro")
        self.assertEqual(fake_client.calls[0][0], "execute_sql_buyerpro")
        self.assertEqual(fake_client.calls[0][1]["sql"], 'select * from public."Converter" limit 1')
        self.assertFalse(rejected["ok"])
        self.assertIn("read-only", rejected["error"])

    def test_agentic_execute_sql_marks_mcp_failure_as_not_ok(self) -> None:
        fake_client = _FakeMcpClient({"content": [{"text": '{"success":false,"error":"relation does not exist"}'}]})

        with patch("app.code_search_agent._dbhub_client", return_value=fake_client):
            result = execute_buyerpro_select(_config(()), 'select * from "Missing" limit 1')

        self.assertFalse(result["ok"])
        self.assertIn("relation does not exist", result["error"])

    def test_agentic_query_logs_is_restricted_to_buyer_containers(self) -> None:
        fake_client = _FakeMcpClient({"content": [{"text": "log line"}]})

        with (
            patch("app.code_search_agent._grafana_client", return_value=fake_client),
            patch("app.code_search_agent._discover_loki_datasource", return_value="loki-uid"),
        ):
            result = query_buyer_logs(_config(()), 'error "upload"')

        self.assertTrue(result["ok"])
        self.assertEqual(fake_client.calls[0][0], "query_loki_logs")
        arguments = fake_client.calls[0][1]
        self.assertEqual(arguments["datasourceUid"], "loki-uid")
        self.assertIn('server="pro-prod2-1"', arguments["logql"])
        self.assertIn('container=~"buyer.*"', arguments["logql"])
        self.assertIn('|= "error \\"upload\\""', arguments["logql"])

    def test_agentic_search_stops_at_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "buyerproback"
            repo.mkdir()
            config = replace(_config((repo,)), code_search_agent_max_steps=2)
            calls = 0

            def ask_model(_messages: list[dict[str, str]]) -> str:
                nonlocal calls
                calls += 1
                return '{"action":"search","query":"upload/normal","reason":"ищу endpoint"}'

            context = collect_agentic_code_context(config, "Где upload/normal?", {"repository": {}}, ask_model)

        self.assertTrue(context["enabled"])
        self.assertEqual(calls, 2)
        self.assertEqual(len(context["steps"]), 2)
        self.assertIn("не собрал дополнительных файлов", context["summary"])


class StorageGenerationJobTest(unittest.TestCase):
    def test_saves_generation_job_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "support.sqlite3"
            init_db(database_path)

            save_generation_job(database_path, "mail-1", "queued")
            queued = get_generation_job(database_path, "mail-1")
            self.assertIsNotNone(queued)
            self.assertEqual(queued["status"], "queued")

            save_generation_job(database_path, "mail-1", "failed", "timeout")
            failed = get_generation_job(database_path, "mail-1")
            self.assertIsNotNone(failed)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "timeout")


class ExcelInspectorTest(unittest.TestCase):
    def test_builds_storage_url(self) -> None:
        url = build_storage_url("https://buyerpro.example.com/", "storage/files/test file.xlsx")

        self.assertEqual(url, "https://buyerpro.example.com/storage/files/test%20file.xlsx")

    def test_parses_xlsx_xml_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.xlsx"
            _write_minimal_xlsx(path)

            parsed = parse_xlsx_xml(path)

        self.assertIn("распаршен как XLSX/XML", parsed["summary"])
        self.assertEqual(parsed["sheets"][0]["name"], "Вводные")
        self.assertEqual(parsed["defined_names"][0]["name"], "AgreementId")
        self.assertEqual(parsed["defined_names"][0]["value"], "П07655")
        self.assertEqual(parsed["interesting_values"][0]["label"], "Договор покупки")
        self.assertEqual(parsed["interesting_values"][0]["value_right"], "П07655")
        self.assertEqual(parsed["download_teo_checks"]["detected_workbook_type"], "converter_source")
        self.assertGreater(parsed["download_teo_checks"]["status_counts"]["failed"], 0)

    def test_download_teo_checks_source_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            reference_path = Path(directory) / "reference.xlsx"
            _write_download_teo_source_xlsx(path)
            _write_template_reference_xlsx(reference_path)

            parsed = parse_xlsx_xml(path, template_reference_path=reference_path)

        checks = parsed["download_teo_checks"]
        item_rows = next(check for check in checks["checks"] if check["name"] == "source_template_item_rows")
        reference_columns = next(
            check for check in checks["checks"] if check["name"] == "source_template_reference_columns"
        )
        self.assertEqual(checks["detected_workbook_type"], "converter_source")
        self.assertEqual(checks["status_counts"]["failed"], 0)
        self.assertEqual(checks["status_counts"]["warning"], 0)
        self.assertEqual(item_rows["details"]["accepted_rows"], 1)
        self.assertEqual(reference_columns["status"], "passed")
        self.assertNotIn("extra_columns", reference_columns["details"])
        self.assertIn("download/teo", parsed["summary"])

    def test_download_teo_template_column_findings_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            reference_path = Path(directory) / "reference.xlsx"
            _write_download_teo_source_xlsx(path)
            _write_template_reference_xlsx(reference_path)
            _replace_xlsx_entry(
                path,
                "xl/worksheets/sheet2.xml",
                lambda content: content.replace(
                    '<c r="B7" t="inlineStr"><is><t>V*Brand</t></is></c>',
                    '<c r="B7" t="inlineStr"><is><t>Brand changed</t></is></c>',
                ).replace(
                    '<c r="AF7" t="inlineStr"><is><t>F*РРЦ</t></is></c>',
                    "",
                ),
            )

            parsed = parse_xlsx_xml(path, template_reference_path=reference_path)

        reference_columns = next(
            check
            for check in parsed["download_teo_checks"]["checks"]
            if check["name"] == "source_template_reference_columns"
        )
        details = reference_columns["details"]
        self.assertEqual(reference_columns["status"], "failed")
        self.assertIn("B: ожидалось `V*Brand`, в файле `Brand changed`", reference_columns["message"])
        self.assertIn("AF `F*РРЦ`", reference_columns["message"])
        self.assertEqual(details["missing_columns"], [{"column": "AF", "expected": "F*РРЦ"}])
        self.assertEqual(
            details["mismatched_columns"],
            [{"column": "B", "expected": "V*Brand", "actual": "Brand changed"}],
        )

    def test_template_column_compare_detects_deleted_shifted_column(self) -> None:
        comparison = _compare_template_header_columns(
            {
                "P": "F*Дирекция",
                "Q": "F*ТК",
                "R": "F*ТпК",
                "S": "F*ТГ подробно",
            },
            {
                "P": "F*ТК",
                "Q": "F*ТпК",
                "R": "F*ТГ подробно",
            },
        )

        self.assertEqual(comparison["missing_columns"], [{"column": "P", "expected": "F*Дирекция"}])
        self.assertEqual(comparison["mismatched_columns"], [])

    def test_download_teo_item_rows_reports_quantity_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            reference_path = Path(directory) / "reference.xlsx"
            _write_download_teo_source_xlsx(path)
            _write_template_reference_xlsx(reference_path)
            _replace_xlsx_entry(
                path,
                "xl/worksheets/sheet2.xml",
                lambda content: content.replace(
                    '<c r="AC8"><v>10</v></c>',
                    '<c r="AB8"><v>10</v></c>',
                ),
            )

            parsed = parse_xlsx_xml(path, template_reference_path=reference_path)

        item_rows = next(
            check
            for check in parsed["download_teo_checks"]["checks"]
            if check["name"] == "source_template_item_rows"
        )
        details = item_rows["details"]
        self.assertEqual(item_rows["status"], "failed")
        self.assertIn("AC `F*Заказ шт`", item_rows["message"])
        self.assertIn("AB `V*Количество`", item_rows["message"])
        self.assertEqual(details["rows_with_model"], 1)
        self.assertEqual(details["quantity_issues"][0]["quantity_column"], "AC")
        self.assertEqual(details["quantity_issues"][0]["neighbor_quantity_value"], "10")


class _FakeMcpClient:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((tool_name, arguments or {}))
        return self.result


def _message() -> dict[str, str]:
    return {
        "sender": "user@example.com",
        "subject": "Ошибка BuyerPro API timeout",
        "sent_at": "2026-06-26",
        "body": "Поставщик сообщает, что BuyerPro API timeout повторяется.",
    }


def _buyerpro_feedback_body() -> str:
    return """Другое - Вопрос по работе с системой
Дата создания:
30.06.2026 11:55:36
Пользователь:
mironov.nikolay@FAMIL.RU
UID обращения:
f72fbbca-e78a-429c-88d7-023085a484ca
№ обращения:
546
/ № предложения: 23
Описание проблемы:
Просто тест обратной связи после хотфикса
BP#546 | UID: f72fbbca-e78a-429c-88d7-023085a484ca"""


def _config(repository_paths: tuple[Path, ...]) -> Config:
    return Config(
        flask_secret_key="test",
        mail_host="",
        mail_port=993,
        mail_username="",
        mail_password="",
        mail_folder="INBOX",
        mail_fetch_limit=20,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="",
        smtp_password="",
        smtp_use_ssl=True,
        support_address="buyerpro-support@famil.ru",
        owner_name="",
        owner_email="owner@example.com",
        ai_provider="offline",
        ai_request_timeout_seconds=300,
        ai_max_output_tokens=700,
        openai_api_key="",
        openai_base_url="",
        openai_model="",
        ollama_base_url="",
        ollama_model="",
        lm_studio_base_url="",
        lm_studio_api_key="",
        lm_studio_model="",
        database_path=Path(":memory:"),
        attachment_dir=Path("data/attachments"),
        max_image_attachment_bytes=5 * 1024 * 1024,
        max_email_attachment_bytes=10 * 1024 * 1024,
        buyerpro_url="https://buyerpro.example.com",
        excel_download_dir=Path("data/excel_downloads"),
        max_excel_download_bytes=50 * 1024 * 1024,
        repository_paths=repository_paths,
        repository_search_limit=5,
        code_search_agent_enabled=True,
        code_search_agent_max_steps=4,
        code_search_agent_max_file_lines=220,
        code_search_agent_min_confidence=0.65,
        mcp_config_path=Path("/missing"),
        mcp_grafana_server="grafana-pro",
        mcp_grafana_url="",
        mcp_grafana_headers={},
        mcp_dbhub_server="dbhub-prod",
        mcp_dbhub_url="",
        mcp_dbhub_headers={},
        mcp_grafana_datasource_uid="",
        mcp_grafana_logql_template='{server="pro-prod2-1", container=~"buyer.*"} |= "{query}"',
        mcp_log_lookback_minutes=60,
        mcp_log_limit=20,
        diagnostics_enabled=False,
    )


def _write_minimal_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>
                <sheet name="Вводные" sheetId="1" r:id="rId1"/>
              </sheets>
              <definedNames>
                <definedName name="AgreementId">'Вводные'!$B$1</definedName>
              </definedNames>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"
                            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <si><t>Договор покупки</t></si>
              <si><t>П07655</t></si>
            </sst>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1">
                  <c r="A1" t="s"><v>0</v></c>
                  <c r="B1" t="s"><v>1</v></c>
                </row>
              </sheetData>
            </worksheet>
            """,
        )


def _write_download_teo_source_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>
                <sheet name="вводные" sheetId="1" r:id="rId1"/>
                <sheet name="Шаблон" sheetId="2" r:id="rId2"/>
              </sheets>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"
                            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
              <Relationship Id="rId2" Target="worksheets/sheet2.xml"
                            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="B1" t="inlineStr"><is><t>П07655</t></is></c></row>
                <row r="2"><c r="B2" t="inlineStr"><is><t>Поставщик</t></is></c></row>
                <row r="3"><c r="B3" t="inlineStr"><is><t>Импорт</t></is></c></row>
                <row r="4"><c r="B4" t="inlineStr"><is><t>01.07.2026</t></is></c></row>
                <row r="6"><c r="B6" t="inlineStr"><is><t>USD</t></is></c></row>
                <row r="7"><c r="B7"><v>90</v></c></row>
                <row r="8"><c r="B8" t="inlineStr"><is><t>Россия</t></is></c></row>
                <row r="9"><c r="B9"><v>1</v></c></row>
                <row r="19"><c r="B19" t="inlineStr"><is><t>Сток</t></is></c></row>
                <row r="20"><c r="B20" t="inlineStr"><is><t>Регулярное</t></is></c></row>
                <row r="25"><c r="B25" t="inlineStr"><is><t>Короб</t></is></c></row>
                <row r="26"><c r="B26" t="inlineStr"><is><t>Подбор</t></is></c></row>
                <row r="27"><c r="B27" t="inlineStr"><is><t>Офис</t></is></c></row>
              </sheetData>
            </worksheet>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="7">
                  <c r="A7" t="inlineStr"><is><t>V*Model</t></is></c>
                  <c r="B7" t="inlineStr"><is><t>V*Brand</t></is></c>
                  <c r="D7" t="inlineStr"><is><t>F*Наименование товара</t></is></c>
                  <c r="T7" t="inlineStr"><is><t>F*ТГ</t></is></c>
                  <c r="AC7" t="inlineStr"><is><t>F*Заказ шт</t></is></c>
                  <c r="AE7" t="inlineStr"><is><t>F*РЦ</t></is></c>
                  <c r="AF7" t="inlineStr"><is><t>F*РРЦ</t></is></c>
                  <c r="AM7" t="inlineStr"><is><t>F*FOB*$</t></is></c>
                  <c r="AN7" t="inlineStr"><is><t>Дополнительная колонка</t></is></c>
                </row>
                <row r="8">
                  <c r="A8" t="inlineStr"><is><t>MODEL-1</t></is></c>
                  <c r="B8" t="inlineStr"><is><t>Brand</t></is></c>
                  <c r="D8" t="inlineStr"><is><t>Product</t></is></c>
                  <c r="T8" t="inlineStr"><is><t>1101.3.1</t></is></c>
                  <c r="AC8"><v>10</v></c>
                  <c r="AE8"><v>99</v></c>
                  <c r="AF8"><v>100</v></c>
                  <c r="AM8"><v>20</v></c>
                </row>
              </sheetData>
            </worksheet>
            """,
        )


def _write_template_reference_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets>
                <sheet name="Шаблон" sheetId="1" r:id="rId1"/>
              </sheets>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"
                            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="7">
                  <c r="A7" t="inlineStr"><is><t>V*Model</t></is></c>
                  <c r="B7" t="inlineStr"><is><t>V*Brand</t></is></c>
                  <c r="D7" t="inlineStr"><is><t>F*Наименование товара</t></is></c>
                  <c r="T7" t="inlineStr"><is><t>F*ТГ</t></is></c>
                  <c r="AC7" t="inlineStr"><is><t>F*Заказ шт</t></is></c>
                  <c r="AE7" t="inlineStr"><is><t>F*РЦ</t></is></c>
                  <c r="AF7" t="inlineStr"><is><t>F*РРЦ</t></is></c>
                  <c r="AM7" t="inlineStr"><is><t>F*FOB*$</t></is></c>
                </row>
              </sheetData>
            </worksheet>
            """,
        )


def _replace_xlsx_entry(path: Path, entry_name: str, replace_content: Any) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries[entry_name] = replace_content(entries[entry_name].decode("utf-8")).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


if __name__ == "__main__":
    unittest.main()

