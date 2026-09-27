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

Сервер использует `ThreadingHTTPServer`, но состояние COM и объектов остаётся
процессным. Адаптер должен сам учитывать потоковую модель целевого API.

## Browser Bridge

`src/browser_bridge/bridge.js` запускает persistent Playwright context. Он
автоматически ищет Brave, Chrome и Edge; при отсутствии использует установленный
Chromium Playwright. Управление строится на locator-ах: role/name, label,
placeholder, test id, text или CSS selector.

Профиль, downloads и screenshots лежат вне Git. Bridge сохраняет storage state,
но эта информация остаётся локальной и считается чувствительной.
## MCP proxy

`src/desktop_agent/mcp/server.mjs` — тонкий stdio-сервер. Он читает токен из
локального файла и проксирует вызов в Desktop Agent. Токен не нужно помещать в
конфигурацию AI-клиента или prompt.

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
