# Подключение MCP-клиента

## Что подключается

Remote Commander даёт транспорт до ПК, а локальный MCP proxy открывает
структурированные возможности Desktop Agent для доверенного AI-клиента.
Это два разных канала; один не заменяет другой.

После установки MCP server находится здесь:

```text
%LOCALAPPDATA%\RdcWorkstationHarness\desktop-agent\mcp\server.mjs
```

Он работает по stdio. Не нужно публиковать порт, копировать токен в prompt или
добавлять секрет в конфигурацию клиента: proxy читает локальный token-файл сам.

## Универсальная конфигурация

Добавьте в MCP-клиент сервер с параметрами:

```text
name: desktop_agent
command: C:\Program Files\nodejs\node.exe
args:
  - %LOCALAPPDATA%\RdcWorkstationHarness\desktop-agent\mcp\server.mjs
transport: stdio
```

Используйте фактический путь `node.exe`, который показывает:

```powershell
(Get-Command node.exe).Source
```
## Доступные tools

- `desktop_health` — проверить локальный router;
- `desktop_capabilities` — все действия по группам с эффектом, browser actions,
  адаптеры и рекомендуемый порядок маршрутизации;
- `desktop_describe` — параметры и пример для действия (`name`), browser action
  (`browser`) или адаптера (`adapter`);
- `desktop_look` — увидеть окно, область или монитор: возвращает картинку
  (MCP image) и формулу перевода пикселей картинки в экранные координаты.
  Окно снимается в фоне без фокуса; для GPU-окон, где фоновый снимок чёрный,
  берутся видимые пиксели экрана;
- `desktop_action` — передать structured action;
- `desktop_send_file` — вернуть локальный файл как MCP resource (токены и
  профили отдать нельзя).

Proxy ждёт ответа не меньше, чем `timeout` самого действия (секунды для
desktop actions, миллисекунды для browser payload), максимум час.

Первый вызов после подключения:

```text
desktop_health({})
```

Затем выполните только read-only проверку, например `status` через
`desktop_action`. Не начинайте с мыши, удаления или изменения BIM-модели.

## Почему установка не правит конфиг клиента автоматически

Форматы MCP-конфигурации и расположение файлов различаются между клиентами и
версиями. Автоматическая правка чужого конфига создаёт больше риска, чем пользы:
можно повредить существующие servers или прописать неверный runtime-путь.
Репозиторий поэтому устанавливает server, но регистрацию оставляет явной.

## Проверка границ

MCP proxy должен запускаться локально на том же Windows-пользователе, что и
Desktop Agent. Не оборачивайте его в публичный HTTP transport и не переносите
локальные токены на другой ПК.

При ошибке сначала проверьте:

```powershell
.\scripts\diagnose.ps1
node.exe $env:LOCALAPPDATA\RdcWorkstationHarness\desktop-agent\mcp\server.mjs
```

Вторую команду запускайте только для диагностики stdio: процесс будет ждать
MCP-сообщения, поэтому завершите его `Ctrl+C` после проверки отсутствия ошибок.
