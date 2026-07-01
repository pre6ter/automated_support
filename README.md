# BAIER PRO

Локальный веб-интерфейс для чтения входящих писем из Яндекс.Почты, подготовки черновиков ответов и отправки проверенных ответов через сайт.

Приложение не отправляет письма автоматически. Оно получает последние письма по IMAP, сохраняет их в локальную SQLite-базу, показывает предложенный ответ в браузере и отправляет письмо только после ручного подтверждения.
Доступ к веб-интерфейсу закрыт логином и паролем: роль `admin` видит письма, чат и управление пользователями, роль `user` видит только чат. Admin задаётся через `.env`; остальные пользователи регистрируются в интерфейсе и получают доступ только после одобрения admin.

## Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:

```bash
AUTH_ADMIN_USERNAME=admin
AUTH_ADMIN_PASSWORD=change-admin-password
MAIL_USERNAME=your.name@company.ru
MAIL_PASSWORD=app-password-from-yandex
SMTP_HOST=smtp.yandex.com
SMTP_PORT=465
SUPPORT_ADDRESS=buyerpro-support@famil.ru
OWNER_NAME=Миронов Николай
OWNER_EMAIL=mironov.nikolay@famil.ru
AI_PROVIDER=lmstudio
```

Для Яндекс 360 лучше использовать пароль приложения, а не основной пароль от аккаунта. Если пароль приложения запрещён корпоративной политикой, попросите администратора разрешить IMAP/OAuth-доступ для почтового клиента.

Приложение загружает и показывает только письма, адресованные на `SUPPORT_ADDRESS`. Письма от `OWNER_EMAIL` игнорируются, чтобы помощник не отвечал на ваши сообщения.

Для отправки ответов используется SMTP. Если `SMTP_USERNAME` и `SMTP_PASSWORD` не заданы, приложение использует `MAIL_USERNAME` и `MAIL_PASSWORD`.

Генерация ответа запускается в фоне: после нажатия кнопки страница письма сразу открывается обратно, показывает статус задачи и автоматически обновляется после завершения.

Если в письме есть вложения, они сохраняются в `ATTACHMENT_DIR` и показываются на странице письма. Изображения и скриншоты (`png`, `jpg`, `gif`, `webp`) дополнительно передаются в модель вместе с текстом письма. В чате можно прикреплять изображения через поле загрузки. Для анализа изображений нужна vision/multimodal модель у выбранного провайдера (`lmstudio`, `openai` или `ollama`); обычная текстовая модель вернёт ошибку провайдера.

Чат использует тот же диагностический контекст, что и анализ почты: read-only поиск по локальным репозиториям, Grafana logs и prod dbhub только по базе `buyerpro`.
Диалоги чата сохраняются в SQLite отдельно для каждого пользователя. Изображения можно прикреплять через поле загрузки или вставлять из буфера обмена в поле вопроса.

Диагностика строится code-first: сначала по вопросу ищутся совпадения во фронтендах (`buyerprofront`, `buyerfront`), затем по найденным терминам проверяются бэкенды (`buyerproback`, `buyerback`). Из этого контекста приложение выводит вероятную предметную/DB-сущность и только потом обращается к логам и `buyerpro` в dbhub.

Если scripted-диагностики недостаточно, приложение может включить agentic diagnostics: модель в несколько шагов просит выполнить только read-only действия `search` и `read_file` внутри `REPOSITORY_PATHS`, просмотреть таблицы `buyerpro`, выполнить один read-only SQL-запрос через `execute_sql_buyerpro` или поискать в Grafana Loki только по `server="pro-prod2-1"` и `container=~"buyer.*"`. После этого ответ генерируется повторно с расширенным контекстом.

Запуск:

```bash
python run.py
```

Откройте в браузере:

```text
http://127.0.0.1:5000
```

## Настройка ИИ

По умолчанию стоит `AI_PROVIDER=lmstudio`: приложение отправляет текст письма в вашу локальную LM Studio через OpenAI-compatible API.

Для LM Studio:

```bash
AI_PROVIDER=lmstudio
AI_REQUEST_TIMEOUT_SECONDS=300
AI_MAX_OUTPUT_TOKENS=700
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_API_KEY=
LM_STUDIO_MODEL=local-model
```

В LM Studio нужно загрузить модель, открыть вкладку локального сервера и запустить OpenAI-compatible server. Если сервер отдаёт конкретный id модели, укажите его в `LM_STUDIO_MODEL`. Если локальная модель отвечает долго на большом диагностическом контексте, увеличьте `AI_REQUEST_TIMEOUT_SECONDS`.
Если локальная модель начинает повторять один и тот же текст, уменьшите `AI_MAX_OUTPUT_TOKENS`; приложение также обрезает повторяющиеся фрагменты ответа перед сохранением.

Для полностью офлайн-шаблона без обращения к модели:

```bash
AI_PROVIDER=offline
```

Для OpenAI API:

```bash
AI_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

Для локального Ollama:

```bash
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1
```

Перед использованием Ollama установите модель, например:

```bash
ollama pull llama3.1
```

## Диагностический контекст

При генерации ответа приложение сначала определяет вероятную категорию проблемы: `Конвертер/Список предложений`, `Согласование ТЭО` или `Другое`. Затем оно собирает read-only контекст из локальных репозиториев, Grafana и dbhub, если включён `DIAGNOSTICS_ENABLED=true`.

Основные настройки:

```bash
REPOSITORY_PATHS=/Users/appleok/Documents/РАБОТА/buyerprofront:/Users/appleok/Documents/РАБОТА/buyerproback:/Users/appleok/Documents/РАБОТА/buyerback:/Users/appleok/Documents/РАБОТА/buyerfront
REPOSITORY_SEARCH_LIMIT=5
CODE_SEARCH_AGENT_ENABLED=true
CODE_SEARCH_AGENT_MAX_STEPS=4
CODE_SEARCH_AGENT_MAX_FILE_LINES=220
CODE_SEARCH_AGENT_MIN_CONFIDENCE=0.65
MCP_GRAFANA_URL=
MCP_GRAFANA_HEADERS_JSON={}
MCP_DBHUB_URL=
MCP_DBHUB_HEADERS_JSON={}
MCP_CONFIG_PATH=~/.cursor/mcp.json
MCP_GRAFANA_SERVER=grafana-pro
MCP_DBHUB_SERVER=dbhub-prod
```

Для переносимой установки без Cursor задайте `MCP_GRAFANA_URL` и `MCP_DBHUB_URL` напрямую. Если MCP-сервер требует заголовки авторизации, передайте их JSON-объектом в `MCP_GRAFANA_HEADERS_JSON` / `MCP_DBHUB_HEADERS_JSON`, например `{"Authorization":"Bearer ... "}`. Если прямой URL не задан, приложение использует совместимый MCP-конфиг из `MCP_CONFIG_PATH` и имена серверов `MCP_GRAFANA_SERVER` / `MCP_DBHUB_SERVER`.

Prod dbhub в приложении ограничен базой `buyerpro`: используются только инструменты `search_objects_buyerpro` и `execute_sql_buyerpro`. Запросы к dbhub проходят через read-only ограничения: мутации и несколько SQL statements запрещены.

## Собственный MCP-сервер

После запуска `python run.py` приложение также отдаёт локальный MCP endpoint:

```text
http://127.0.0.1:5000/mcp
```

Пример подключения в Cursor `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "automated-support": {
      "url": "http://127.0.0.1:5000/mcp"
    }
  }
}
```

Сервер предоставляет read-only инструменты `classify_support_issue`, `collect_chat_diagnostics`, `collect_message_diagnostics`, `get_message_context`, `search_repositories`, `inspect_offer_number` и `execute_dbhub_select`. Инструменты переиспользуют существующую диагностику проекта; SQL через dbhub дополнительно проходит ту же проверку на read-only.

Для диагностики Excel-файлов BuyerPro можно включить скачивание XLSX из storage:

```bash
BUYERPRO_URL=https://buyerpro.company.ru
EXCEL_DOWNLOAD_DIR=data/excel_downloads
MAX_EXCEL_DOWNLOAD_MB=50
```

Приложение берёт путь из `purch_req_request.local_file` для файлов согласования или из `Converter.localFile` для файлов предложения и скачивает файл по адресу `BUYERPRO_URL/storage/<path>`. XLSX раскрывается как ZIP/XML: бот получает список листов, именованные диапазоны, интересные поля и примеры непустых ячеек.

ИИ также получает основные определения системы. Например, «Номер предложения» понимается как два числа через точку или запятую (`12177.9` / `12177,9`) и связывается с `Converter.brandId + Converter.number`, `converter_id`, `purch_req_request`, `production_order` и похожими комбинациями в таблицах.

Если в вопросе или письме указан номер предложения, приложение выполняет read-only lookup в `buyerpro`: ищет запись в `public."Converter"` по `brandId + number`, затем связанные строки в `purch_req_request` и `production_order` по `converter_id`.

## Безопасность

- Не коммитьте `.env`: он уже добавлен в `.gitignore`.
- Не включайте автоотправку ответов без отдельного подтверждения.
- Проверяйте черновик перед отправкой: ИИ может ошибиться в категории или интерпретации логов.
- Учитывайте корпоративные правила: текст писем может содержать персональные данные или коммерческую тайну.
- Если используете внешний AI API, письма будут отправляться этому провайдеру для генерации ответа.
- При `AI_PROVIDER=lmstudio` письма уходят только в локальную LM Studio на вашем компьютере.
