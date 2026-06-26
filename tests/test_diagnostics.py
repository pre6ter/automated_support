import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.ai import (
    _chat_messages,
    _openai_messages,
    _parse_support_response,
    _prioritized_diagnostic_context,
    generate_chat_answer,
    generate_support_response,
)
from app.code_intelligence import collect_code_entity_context
from app.config import Config
from app.diagnostics import (
    _buyerpro_flow_queries,
    _chat_diagnostic_text,
    _entity_lookup_queries,
    _extract_lookup_identifiers,
    _offer_number_queries,
)
from app.domain_knowledge import domain_knowledge_prompt
from app.domain_knowledge import parse_offer_numbers
from app.excel_inspector import build_storage_url, parse_xlsx_xml
from app.mcp_client import validate_readonly_sql
from app.repository_context import collect_repository_context, extract_search_terms
from app.storage import get_generation_job, init_db, save_generation_job
from app.taxonomy import ProblemCategory, guess_category, normalize_category


class TaxonomyTest(unittest.TestCase):
    def test_normalizes_aliases(self) -> None:
        self.assertEqual(normalize_category("Аксанта"), ProblemCategory.AXAPTA)
        self.assertEqual(normalize_category("buyer_pro"), ProblemCategory.BUYERPRO)

    def test_guesses_category_from_text(self) -> None:
        category = guess_category("Ошибка BuyerPro", "Поставщик не может открыть pro.famil.ru")
        self.assertEqual(category, ProblemCategory.BUYERPRO)


class AiParsingTest(unittest.TestCase):
    def test_parses_json_response(self) -> None:
        response = _parse_support_response(
            """
            ```json
            {
              "category": "integrations",
              "confidence": 0.81,
              "probable_problem": "Ошибка обмена",
              "evidence": ["В письме указан API timeout"],
              "next_checks": ["Проверить логи"],
              "draft": "Здравствуйте! Проверим обмен."
            }
            ```
            """,
            _message(),
        )

        self.assertEqual(response.category, ProblemCategory.INTEGRATIONS)
        self.assertEqual(response.confidence, 0.81)
        self.assertEqual(response.evidence, ["В письме указан API timeout"])
        self.assertEqual(response.draft, "Здравствуйте! Проверим обмен.")

    def test_falls_back_for_plain_text(self) -> None:
        response = _parse_support_response("Здравствуйте! Уточните номер заказа.", _message())
        self.assertEqual(response.category, ProblemCategory.BUYERPRO)
        self.assertIn("Уточните номер заказа", response.draft)

    def test_chat_messages_include_history_and_question(self) -> None:
        messages = _chat_messages(
            [{"role": "user", "content": "Первый вопрос"}],
            "Второй вопрос",
            {"sources": [{"title": "dbhub prod", "summary": "buyerpro only"}]},
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Номер предложения", messages[0]["content"])
        self.assertIn("Converter.brandId", messages[0]["content"])
        self.assertIn("Диагностический контекст", messages[0]["content"])
        self.assertIn("buyerpro only", messages[0]["content"])
        self.assertIn("excel_file_xml_inspection", messages[0]["content"])
        self.assertEqual(messages[-2]["content"], "Первый вопрос")
        self.assertEqual(messages[-1]["content"], "Второй вопрос")

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

    def test_known_kaspersky_file_issue_uses_template_for_email(self) -> None:
        message = {
            **_message(),
            "subject": "Не выгружается файл из BuyerPro",
            "body": "Не получается скачать файл 12 МБ из БайерПро.",
        }

        response, provider, model = generate_support_response(_config(()), message)

        self.assertEqual(provider, "rule")
        self.assertEqual(model, "known-issue")
        self.assertEqual(response.category, ProblemCategory.BUYERPRO)
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


class SqlValidatorTest(unittest.TestCase):
    def test_allows_select(self) -> None:
        self.assertEqual(validate_readonly_sql("select * from public.orders limit 1;"), "select * from public.orders limit 1")

    def test_rejects_mutation(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_sql("update public.orders set status = 'x'")

    def test_rejects_multiple_statements(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_sql("select 1; select 2")


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

    def test_offer_number_expands_repository_terms(self) -> None:
        message = {
            "subject": "Номер предложения 12177.9",
            "body": "Проверьте номер предложения 12177,9 в заявке.",
        }

        terms = extract_search_terms(message, ProblemCategory.BUYERPRO)

        self.assertIn("12177.9", terms)
        self.assertIn("12177,9", terms)
        self.assertIn("converter_id", terms)
        self.assertIn("purch_req_request", terms)
        self.assertIn("production_order", terms)

    def test_parse_offer_numbers(self) -> None:
        self.assertEqual(parse_offer_numbers("статус 24392.4 и 12177,9"), [(24392, 4, "24392.4"), (12177, 9, "12177,9")])

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

            context = collect_repository_context(config, _message(), ProblemCategory.BUYERPRO)

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
                ProblemCategory.BUYERPRO,
            )

            self.assertEqual(context["flow"], "frontend_code -> backend_code -> db/logs")
            self.assertTrue(context["frontend"]["matches"])
            self.assertTrue(context["backend"]["matches"])
            self.assertEqual(context["entities"][0]["key"], "offer")
            self.assertIn("Converter", context["db_terms"])


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


def _message() -> dict[str, str]:
    return {
        "sender": "user@example.com",
        "subject": "Ошибка BuyerPro API timeout",
        "sent_at": "2026-06-26",
        "body": "Поставщик сообщает, что BuyerPro API timeout повторяется.",
    }


def _config(repository_paths: tuple[Path, ...]) -> Config:
    return Config(
        flask_secret_key="test",
        mail_host="",
        mail_port=993,
        mail_username="",
        mail_password="",
        mail_folder="INBOX",
        mail_fetch_limit=20,
        support_address="buyerpro-support@famil.ru",
        owner_name="",
        owner_email="owner@example.com",
        ai_provider="offline",
        ai_request_timeout_seconds=300,
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
        buyerpro_url="https://buyerpro.example.com",
        excel_download_dir=Path("data/excel_downloads"),
        max_excel_download_bytes=50 * 1024 * 1024,
        repository_paths=repository_paths,
        repository_search_limit=5,
        mcp_config_path=Path("/missing"),
        mcp_grafana_server="grafana-pro",
        mcp_dbhub_server="dbhub-prod",
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


if __name__ == "__main__":
    unittest.main()

