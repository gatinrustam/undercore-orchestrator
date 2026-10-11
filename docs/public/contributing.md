# Участие в разработке

Начните с [архитектуры](architecture.md), [гарантий жизненного цикла](lifecycle.md),
[правил совместимости](compatibility.md) и [контракта API](api.md). Тесты не требуют
настоящих серверов, соседнего закрытого репозитория или VPN-ключей.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps .
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_contracts.py
.venv/bin/python scripts/check_architecture.py
.venv/bin/python -m mypy
.venv/bin/ruff check orchestrator scripts tests
.venv/bin/ruff format --check orchestrator scripts tests
```

CI выполняет эти проверки на Python 3.12/Linux и собирает архив точного коммита.
Ручная проверка на реальном узле — отдельный этап, не часть обычного pytest.

## Где менять код

| Задача | Место |
|---|---|
| Модели входа/выхода | `orchestrator/domain/contracts.py` |
| HTTP-граница | `orchestrator/interfaces/http/` |
| Команды оператора | `orchestrator/interfaces/cli/` |
| Выдача и восстановление | `orchestrator/application/` |
| Протокольный драйвер | `orchestrator/infrastructure/drivers/` |
| Интерфейсы журналов | `orchestrator/application/repository_ports.py` |
| Хранение и транзакции | `orchestrator/infrastructure/sqlite/` |
| Модели таймаутов и конфигурации | `orchestrator/config/` |
| Загрузка конфигурации и секретов | `orchestrator/infrastructure/settings.py`, `credentials.py` |
| Сборка реализаций | `orchestrator/bootstrap.py` |

Корневые runtime/settings/assignments и другие короткие модули — совместимые
импорты старых точек запуска. Новую реализацию добавляйте в соответствующий слой.
Не удаляйте совместимость без описанного перехода.

Проверка импортов использует contracts/architecture.json. Новые запрещённые
зависимости и устаревшие исключения блокируют CI. Список исключений не обновляется
автоматически; сейчас он пуст. Mypy проверяет типы на границах слоёв
и соответствие реализаций портам; область проверки явно задана в pyproject.toml.

## Изменение контракта

Обновляйте код, машинную схему, публичные примеры и тесты вместе.
`python scripts/export_openapi.py` пересобирает статическую OpenAPI для новых
интеграций; `--check` проверяет, что артефакт актуален. `check_contracts.py` проверяет
также прежнюю JSON Schema, каталог кодов и контрольные суммы fixture агента.
Новый HTTP-код или другая семантика повтора требуют отдельного анализа совместимости.

Тестируйте наблюдаемое поведение: повтор после потери ответа, истечение срока,
отзыв, перезапуск незавершённой операции. Не изменяйте pinned fixture агента только
ради прохождения теста. Для нового протокола используйте [руководство](drivers.md).

## Pull request

Опишите проблему, получившееся поведение, совместимость, миграции и проверки.
Один PR должен иметь понятную цель. Для рефакторинга сначала зафиксируйте важное
поведение тестом, затем меняйте реализацию. Не добавляйте биллинг, пользовательскую
авторизацию или зависимости от админки сайта.

Документы размещаются в docs/public или docs/internal; README в корне — короткий
вход в документацию. Реальные настройки, базы, ключи и конфигурации не коммитятся.
Собственный код проекта распространяется по [MIT](https://github.com/UndercoreCo/orchestrator/blob/main/LICENSE). При добавлении
стороннего кода сохраняйте сведения об авторстве и его лицензии.

## Сайт документации

Публичный сайт: https://undercoreco.github.io/orchestrator/.
VitePress читает только docs/public; docs/internal не входит в сборку сайта.
Публичный GitHub-репозиторий остаётся публичным: исключение из сайта не закрывает
доступ к уже опубликованным файлам репозитория.

Для редактирования используйте Node.js 24 и npm:

```sh
npm ci
npm run docs:dev
npm run docs:build
npm run docs:preview
```

Markdown остаётся единственным источником содержимого. Ссылки на код и машинные
контракты ведут в GitHub, ссылки между руководствами — на страницы сайта.
Поиск встроенный, локальный; внешняя поисковая служба не нужна.

GitHub Actions проверяет сборку в pull request. После изменения публичной
документации в main автоматически публикуется только каталог .vitepress/dist.
API, службы оркестратора и VPN-узлы этот workflow не разворачивает.
