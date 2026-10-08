# Обслуживание самостоятельного оркестратора

Для новой установки используйте [быстрый старт](quickstart.md). Управление узлами теперь только через локальный CLI; старый `admin_token_file` игнорируется. Параметры `nodes` нужны лишь для импорта прежней установки.

# Эксплуатация

## Размещение

| Путь | Содержимое |
|---|---|
| `/opt/vpn-orchestrator/releases/vVERSION-COMMIT/` | Неизменяемый релиз со своей `.venv` |
| `/opt/vpn-orchestrator/current` | Символическая ссылка на рабочий релиз |
| `/etc/vpn-orchestrator/settings.json` | Начальные настройки узлов, не из Git |
| `/etc/vpn-orchestrator/*.token` | Отдельные ключи, права 0600 |
| `/var/lib/vpn-orchestrator/assignments.sqlite3` | Журнал назначений и переключений |
| `/var/backups/undercore-orchestrator-COMMIT/` | Приватный снимок перед обновлением |

Сервис работает от `vpn-orchestrator`, API слушает только loopback:8792.
Настройки и ключи должны читаться этим пользователем; каталог состояния — 0700,
SQLite — 0600. Код принадлежит root и недоступен сервису для записи.

Пример без настоящих адресов: [settings.example.json](../../config/settings.example.json).
`schema_version` — версия настроек, `id` и `server_id` узла стабильны.
`active` допускает новые назначения, `draining` обслуживает существующие,
`disabled` исключает узел из обслуживания. Отключение узла в настройках не отзывает
уже выданные peer на сервере. Не удалять записи узлов, на которые ссылается журнал.
`check-config` проверяет также незавершённые цели переключений.

## Команды

```sh
sudo orchestratorctl check-config
sudo orchestratorctl check-config --probe
sudo orchestratorctl health
sudo orchestratorctl nodes
```

`--probe` выполняет только проверку health/идентичности узлов. Обычный `health`
проверяет процесс оркестратора. Структурная проверка примера не требует ключей:

```sh
python -m orchestrator --settings config/settings.example.json check-config --structure-only
```

Примеры управления (фиктивные идентификаторы; реальные команды изменяют доступ):

```sh
sudo orchestratorctl connection create --device device-example-01 --name MacBook --expires-at 2027-01-01T00:00:00.000Z
sudo orchestratorctl connection show CONNECTION_ID --device device-example-01
sudo orchestratorctl connection renew CONNECTION_ID --device device-example-01 --expires-at 2027-02-01T00:00:00.000Z --key renewal-example-01
sudo orchestratorctl connection disable CONNECTION_ID --device device-example-01
sudo orchestratorctl connection enable CONNECTION_ID --device device-example-01
sudo orchestratorctl connection recover CONNECTION_ID --device device-example-01 --revision 1
sudo orchestratorctl reconcile
```

CLI не проверяет оплату: это инструмент доверенного оператора. Вывод ограничен
метаданными; приватный конфигурационный документ не печатается. `reconcile`
продолжает уже начатые переключения, может вызвать мутации на узлах. Обычно его
запускает `vpn-orchestrator-recovery.timer` каждые 30 секунд после завершения прохода.

## Первый запуск на новом сервере

Нужен Linux с systemd, Python 3.12, venv и HTTPS-доступом к PyPI/агентам. Скачать
архив выбранного GitHub Release и SHA256SUMS. Проверить хеш и содержимое с помощью
`scripts/update.py:inspect_archive` перед извлечением. Установить код в новый каталог
`/opt/vpn-orchestrator/releases/vVERSION-COMMIT`, создать там `.venv`, установить
`requirements.lock`. Создать пользователя `vpn-orchestrator`, каталоги и приватные
настройки с настоящими узлами; не копировать пример с disabled-узлом как рабочий.

Выполнить `python -m orchestrator check-config --probe` из каталога релиза от
пользователя сервиса с `ORCHESTRATOR_SETTINGS=/etc/vpn-orchestrator/settings.json`.
Создать `current`, установить пять unit-файлов из `deploy`, wrapper `orchestratorctl`
в `/usr/local/bin` (0755), выполнить `systemctl daemon-reload` и
`systemctl enable --now vpn-orchestrator.service vpn-orchestrator-recovery.timer vpn-orchestrator-leases.timer`.
При первом запуске создаётся пустой журнал. Настроить доступ backend через локальную
сеть/reverse proxy с HTTPS и ограничением источников, затем проверить health и nodes.
Автоматизированный updater ниже предназначен для уже установленного сервиса.

## Выпуск и обновление

1. Изменить код и `VERSION`, выполнить тесты. Закоммитить и отправить в main.
2. Создать тег точно `vVERSION`, отправить его. GitHub Actions повторно проверит
   тесты/контракт и опубликует архив плюс `SHA256SUMS` в GitHub Releases.
3. На компьютере оператора с `gh`, SSH-доступом root и доверенным host key:

```sh
python3 scripts/deploy.py --release v0.1.0 --host root@YOUR_SERVER
```

Первый перенос существующего сервиса без release manifest требует дополнительно
`--adopt-existing`. Для обычных следующих релизов флаг не нужен.
GitHub-токен остаётся на компьютере оператора. Новый релиз не разворачивается от
произвольного push: явная команда выбирает точную проверенную версию.

Updater проверяет SHA256, список/типы файлов, версию, конфигурацию и отсутствие
неявной миграции SQLite на копии. Устанавливает зависимости в отдельную `.venv`
до остановки API. Затем приостанавливает обработчик, снимает согласованную копию
журнала, меняет ссылку/units и проверяет health новой версии. Настройки и рабочий
SQLite не заменяются; сайты и VPN-агенты не перезапускаются.

При ошибке после переключения возвращаются старые код/units и запуск сервиса.
Живой журнал **не откатывается**: он может содержать уже выполненные операции.
Версия 0.2 добавляет таблицы leases/cache/retired bindings, не изменяя существующие
назначения и переключения. Иные изменения этих данных/схем отклоняются на копии.
После включения leases допустим только lease-aware rollback; см. [границы](node-leases.md).

`SIGTERM`/разрыв SSH обрабатываются для попытки отката; сбой питания или `SIGKILL`
невозможно гарантированно обработать. Тогда оператор сверяет `current`, units,
health и приватный снимок. Никогда не заменять рабочую SQLite старой копией без
сверки реальных peer на узлах. Повтор незавершённой установки в уже созданный
каталог намеренно запрещён: сначала проверить результат предыдущей попытки.

## Резервирование и наблюдение

SQLite снимать через online backup API либо при остановленных API и worker.
Сохранять отдельно настройки/ключи, units, release manifest и сведения о версии.
Копии содержат служебные секреты: ограничить права, шифровать и выносить с машины.
Из архивов кода исключать `.venv`, `venv`, `.git`, `__pycache__`. Предрелизная копия
на том же диске не заменяет независимый backup.

```sh
systemctl status vpn-orchestrator.service
systemctl list-timers vpn-orchestrator-recovery.timer
journalctl -u vpn-orchestrator.service -u vpn-orchestrator-recovery.service --since '1 hour ago'
```

Access log приложения выключен. Не включать отладочное логирование заголовков и
тел конфигурационных ответов в прокси/HTTP-клиентах. Текущий updater рассчитан на
один экземпляр оркестратора и локальную SQLite; несколько активных копий с разными
журналами нельзя запускать как балансируемые реплики.


## Локальный реестр узлов

После первого запуска определения `nodes` импортируются в SQLite. Далее только
локальный CLI меняет реестр; JSON не перезаписывает его. После ротации ключей
bootstrap-файлы больше не нужны, если на них не ссылается действующий реестр.
Старый `admin_token_file` игнорируется, сайт не имеет полномочий управления.

```sh
sudo -u vpn-orchestrator undercore-orchestrator list servers
sudo -u vpn-orchestrator undercore-orchestrator check server NODE_ID
sudo -u vpn-orchestrator undercore-orchestrator update server NODE_ID --file /private/change.json
sudo -u vpn-orchestrator undercore-orchestrator restore server NODE_ID
```

Файл изменения приватный (0600), содержит полный набор полей и expected_revision.
Restore отзывает старые перенесённые доступы до открытия узла. Backup должен
включать журнал, актуальные секреты, настройки и deployment units. Команда backup
собирает журнал/настройки/секреты; версию релиза и units сохраняйте вместе с ними.
