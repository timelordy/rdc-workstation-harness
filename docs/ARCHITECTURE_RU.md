# Архитектура

## Цель

Дать AI-клиенту универсальный, но контролируемый доступ к доверенной Windows-
станции. Harness не должен состоять из сотни жёстко прошитых сценариев и не
должен без необходимости двигать видимую мышь.

## Слои

```text
AI client
   |
   +-- Remote Desktop Commander Remote
   |      +-- файлы
   |      +-- процессы и терминал
   |      +-- транспорт до рабочего ПК
   |
   +-- MCP stdio proxy
          |
          +-- Desktop Agent :17322
                 +-- generic runtime
                 +-- app adapters
                 +-- Browser Bridge :17321
```

Remote Desktop Commander и собственные локальные службы решают разные задачи.
RDC даёт надёжный транспорт и операции ОС. Desktop Agent добавляет семантическое
управление окнами, COM, адаптеры и единый локальный action router. Browser Bridge
работает с DOM, а не с координатами экрана.
## Desktop Agent

`src/desktop_agent/agent.py` — loopback HTTP router. Он принимает только
`POST /action` с локальным токеном. `GET /health` не содержит секретов и нужен
watchdog/диагностике.

Основные группы возможностей:

- UI Automation: поиск элементов, дерево, invoke, value и semantic click;
- Win32: окна, сообщения и фоновый захват;
- generic runtime: subprocess, Python, PowerShell и process probing;
- COM: создание/подключение объектов и вызов методов;
- adapters: горячая загрузка небольшой логики конкретного приложения;
- Browser Bridge forwarding;
- staging готовых файлов для совместимого MCP-клиента;
- физический ввод только с явным разрешением конкретного запроса.

HTTP-сервер принимает параллельные запросы. COM и адаптеры выполняются на одном
STA owner thread, UIA/Win32/physical input — на отдельном MTA owner thread.
Создание, использование и освобождение COM handles остаются в одном apartment.
Браузер и процессы не занимают эти desktop workers.

Очередь каждого desktop worker ограничена 256 ожидающими вызовами и 10 секундами
ожидания старта. Истёкшая команда не исполняется позже; ответ содержит
`QUEUE_TIMEOUT` и `action_started: false`. Если timeout произошёл внутри уже
запущенного адаптера, `action_started: true` относится к внешнему adapter call.
Уже начатую нативную COM/UIA операцию очередь принудительно не прерывает.

Для UIA внутри адаптера используйте `context['uia_action'](request)`, который
возвращает обычные данные. Raw helpers `find_uia`/`window_info` в adapter context
отклоняются: UIA wrappers не должны покидать свой owner thread. Прикладной SDK
может требовать ещё более узкий контекст, например Revit ExternalEvent.

## Каталог действий и самоописание

`src/desktop_agent/catalog.py` — единственное описание всех действий: группа,
эффект (`read` / `write` / `exec` / `physical`), параметры, обязательные поля и
пример. Из него же строятся диспетчер, `capabilities` и `describe`, поэтому
модель видит ровно то, что агент умеет. Тест `test_catalog.py` падает, если
обработчик добавлен без описания или наоборот, а также если список browser
actions разошёлся с реализацией Browser Bridge или типизированным MCP tool.

Типичный путь модели:

```text
desktop_capabilities            -> все группы, browser actions, адаптеры
desktop_describe {adapter}      -> что умеет адаптер и что он меняет
desktop_describe {name}         -> параметры действия и пример
desktop_action {...}            -> вызов
```

Ошибки возвращаются структурно: `error`, `type`, `hint` и по возможности
`did_you_mean`, `valid_actions` или `params`. Статусы: 400 — ошибка запроса,
403 — нужно явное разрешение (`allow_physical`, `commit`, защищённый файл),
500 — сбой целевого приложения.

## Browser Bridge

`src/browser_bridge/bridge.js` запускает persistent Playwright context. Он
автоматически ищет Brave, Chrome и Edge; при отсутствии использует установленный
Chromium Playwright. Управление строится на locator-ах: role/name, label,
placeholder, test id, text или CSS selector. Locator по умолчанию строгий;
`nth` требуется для явного позиционного выбора.

`browser_session.js` управляет browser context, очередью и живыми идентификаторами
вкладок/frames. Генерация ID уникальна для процесса/session, поэтому старый ID
не указывает на новый объект после рестарта. `bridge.js` оставляет transport,
loopback/token boundary и создание локальных каталогов.

Профиль, downloads и screenshots лежат вне Git. Bridge сохраняет storage state,
но эта информация остаётся локальной и считается чувствительной.
## MCP proxy

`src/desktop_agent/mcp/server.mjs` — тонкий stdio-сервер. Он читает токен из
локального файла и проксирует вызов в Desktop Agent. Токен не нужно помещать в
конфигурацию AI-клиента или prompt.

## Skills и состояние задачи

`skills.py` обнаруживает только настроенные корни и явно доверенные project skills,
проверяет YAML/JSON, ограничивает чтение и возвращает метаданные либо содержимое
отдельного skill. Полный ID включает источник; коллизии имён видны в диагностике.
`contract.json` — необязательное расширение harness, не выдача полномочий.

`tasks.py` хранит цель, снимки инструкций и контракта, критерии, свидетельства и
состояние в SQLite вне Git. Каждая запись транзакционна; ручные наблюдения и
серверные file/CSV-проверки имеют разные типы свидетельств. Перед завершением
артефакты читаются заново. Возвращается покрытие всех проверок, включая пропущенные.

`desktop_skills`, `desktop_prepare_task`, `desktop_task` и команды `pc-agent`
обращаются к тем же HTTP actions. Логика реестра и задач не зависит от Windows UIA
и тестируется отдельно. Каталог действий содержит эффекты этих операций.

Журнал не оборачивает автоматически произвольные `exec`, COM, browser или UIA
вызовы. Он не обеспечивает exactly-once, полномочия или автоматическое продолжение
команд. После восстановления нужны новые наблюдения и проверка неизвестных исходов.
Полный контракт и подключение клиента: [SKILLS_RU.md](SKILLS_RU.md).

## App adapters

Адаптеры лежат в `src/desktop_agent/adapters`. Core не должен разрастаться
логикой AutoCAD, Navisworks и каждого следующего приложения.

Предпочтительный adapter flow:

```text
PID / executable
      |
public SDK? --------> adapter
COM? ---------------> adapter
CDP / RPC / socket? -> adapter
CLI / scripting? ----> adapter
UIA? ----------------> semantic control
none ----------------> visual/physical fallback
```

В репозитории есть AutoCAD COM adapter. Новые интеграции должны
использовать публичный интерфейс приложения и иметь понятную границу side effects.

Контракт адаптера — модуль с тремя именами:

```python
DESCRIPTION = "Что за приложение и как адаптер к нему подключается."
ACTIONS = {
    "status": {"effect": "read", "summary": "...", "params": {}},
    "export": {"effect": "write", "summary": "...", "params": {"path": "..."}},
}

def handle(payload, context):
    ...
```

`adapter_list` и `describe {adapter}` показывают `DESCRIPTION` и `ACTIONS`,
`adapter_call` отклоняет неизвестное действие со списком допустимых, а
`adapter_install` не принимает модуль без манифеста и возвращает предыдущую
версию файла, если новая не загрузилась. Модули кешируются по mtime и
перезагружаются только после изменения файла.

## Жизненный цикл процессов

Windows Task Scheduler запускает по одному watchdog на компонент. Watchdog:

1. проверяет loopback health endpoint;
2. не создаёт второй worker, если первый уже жив;
3. запускает worker и пишет stdout/stderr в локальный log;
4. после падения делает короткую паузу и запускает его снова.

Установщик использует фиксированные имена `RDC Harness - ...`, поэтому повторный
запуск обновляет те же задачи, а не добавляет новые Startup-записи.

## Локальные данные

```text
%LOCALAPPDATA%\RdcWorkstationHarness\  runtime, venv, logs, browser profile
%USERPROFILE%\.chatgpt-desktop-agent\  desktop/browser tokens
%USERPROFILE%\.desktop-commander-device\ identity конкретного RDC-устройства
%USERPROFILE%\Downloads\rdc-harness*\  создаваемые artifacts/downloads
```

Ни один из этих каталогов не должен попадать в Git. Идентичность RDC и
браузерный профиль нельзя переносить коллеге; они создаются заново.

## Сетевая граница

Порты 17321–17322 предназначены только для loopback. Для удалённой связи
используется транспорт Remote Desktop Commander, а не прямой проброс HTTP.
`diagnose.ps1` отмечает ошибкой любой listener этих портов не на `127.0.0.1`/`::1`.

## Осознанно не включено

- копирование основного браузерного профиля с паролями и cookies;
- локальные токены, logs, screenshots и output-файлы;
- две конкурирующие схемы автозапуска;
- экспериментальный Codex app-server keepalive на нестабильном интерфейсе;
- process injection и изменение памяти приложений.
