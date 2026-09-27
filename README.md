# RDC Workstation Harness

[![CI](https://github.com/timelordy/rdc-workstation-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/timelordy/rdc-workstation-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Набор для развёртывания управляемого AI‑рабочего места на Windows вокруг
Remote Desktop Commander: файловая система и процессы через RDC, браузер через
Playwright, обычные окна через UI Automation/Win32, приложения — через COM или
небольшие адаптеры. Revit‑мост поставляется отдельно и необязателен.

> Это локальный привилегированный инструмент для доверенного рабочего ПК.
> Он не является сетевым API, сервером общего доступа или средством скрытого
> администрирования. Все собственные HTTP‑службы жёстко слушают только
> `127.0.0.1` и требуют локальные токены.

## Что входит

| Слой | Назначение | По умолчанию |
|---|---|---|
| Remote Desktop Commander | файлы, терминал, процессы, связь с клиентом | устанавливается опционально |
| Desktop Agent | UIA, Win32, COM, Python/PowerShell, адаптеры | `127.0.0.1:17322` |
| Browser Bridge | DOM/ARIA‑управление браузером через Playwright | `127.0.0.1:17321` |
| MCP proxy | отдаёт Desktop Agent MCP‑совместимому клиенту | stdio |
| Revit Bridge | выполняет C# на главном потоке Revit | `127.0.0.1:17323` |
| `rdc_web.py` | предохранитель для заметных браузерных действий | CLI |
## Архитектура

```text
AI client / MCP client
          |
Remote Desktop Commander Remote
          |
   Windows workstation
          |
          +-- desktop-agent :17322
          |      +-- UI Automation / Win32
          |      +-- Python / PowerShell / subprocess
          |      +-- COM and app adapters
          |      +-- optional Revit adapter -> :17323
          |
          +-- browser-bridge :17321
                 +-- Playwright locators
                 +-- persistent local browser profile
```

Маршрутизация строится снизу вверх по стабильности: native API/SDK → COM/CDP/CLI
→ адаптер → Playwright → UIA → Win32 → физический ввод. Реальная мышь и
клавиатура являются последним вариантом и по умолчанию запрещены.

## Быстрый запуск

Требования: Windows 10/11, Python 3.10+, Node.js 18+ и Git.

```powershell
git clone https://github.com/timelordy/rdc-workstation-harness.git
cd rdc-workstation-harness
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1 -InstallRemoteCommander
```
При первой установке Remote Commander отдельно сопрягите именно этот ПК:

```powershell
.\scripts\pair-remote-commander.ps1
```

`%USERPROFILE%\.desktop-commander-device` содержит идентичность конкретного
устройства. Её нельзя коммитить, архивировать в handoff или переносить коллеге.
На каждом ПК выполняется своё сопряжение.

Проверка после установки:

```powershell
.\scripts\diagnose.ps1
Invoke-RestMethod http://127.0.0.1:17322/health
Invoke-RestMethod http://127.0.0.1:17321/health
.\scripts\show-mcp-config.ps1
```

Установщик:

- разворачивает runtime в `%LOCALAPPDATA%\RdcWorkstationHarness`;
- создаёт отдельное Python‑окружение;
- ставит только необходимые Node/Python‑зависимости;
- генерирует два 256‑битных токена и не выводит их в консоль;
- регистрирует по одной задаче автозапуска на компонент;
- сохраняет токены и браузерный профиль при обычном обновлении;
- не копирует идентичность Remote Commander между машинами.

Повторный запуск `install.ps1` используется и как обновление. Для смены токенов:

```powershell
.\scripts\install.ps1 -InstallRemoteCommander -RotateTokens
```
## Выбор браузера

В режиме `auto` мост ищет Brave, Chrome и Edge, а затем использует локальный
Chromium Playwright, если он установлен. Можно выбрать явно:

```powershell
.\scripts\install.ps1 -Browser brave
.\scripts\install.ps1 -Browser chrome
.\scripts\install.ps1 -Browser edge
.\scripts\install.ps1 -Browser bundled -InstallBundledChromium
```

Профиль моста отдельный. Личные пароли и cookies из обычного профиля браузера
автоматически не копируются. В нужные сайты коллега входит сам в своём профиле.

## Revit 2022–2024

Revit‑мост не ставится базовым установщиком:

```powershell
.\scripts\install-revit-bridge.ps1 -RevitYear 2022
```

Он собирается локально против `RevitAPI.dll` установленной версии и после
перезапуска Revit слушает только loopback. Текущая реализация использует
.NET Framework и не заявляет совместимость с Revit 2025+; для них нужен
отдельный порт под .NET 8.

## Управление

```powershell
.\scripts\start.ps1 -IncludeRemoteCommander
.\scripts\stop.ps1 -IncludeRemoteCommander
.\scripts\diagnose.ps1
.\scripts\uninstall.ps1
```

Удаление по умолчанию сохраняет токены, браузерный профиль и идентичность
Remote Commander. Их можно удалить явными ключами — см. `-Help` скрипта.

## Навигатор обновлений

По умолчанию установщик регистрирует задачу `RDC Harness - Update Check`.
Она проверяет обновления при входе в Windows и раз в день, но показывает
уведомление только один раз для каждой новой версии.

- `stable` — рекомендуемый канал, отслеживает GitHub Releases.
- `main` — отслеживает каждый новый commit в ветке `main`.

Ручная проверка и установка:

```powershell
& "$env:LOCALAPPDATA\RdcWorkstationHarness\maintenance\check-update.ps1"
& "$env:LOCALAPPDATA\RdcWorkstationHarness\maintenance\update.ps1"
```

Для разработчика выпуск обновления выглядит так: изменить `VERSION` и
`CHANGELOG.md`, смержить в `main`, затем создать GitHub Release с тегом
`vX.Y.Z`. Пользователи stable-канала увидят уведомление автоматически.
Подробно: [обновления](docs/UPDATES_RU.md).

## Безопасная граница

- Собственные службы намеренно привязаны к `127.0.0.1`; не меняйте это на
  `0.0.0.0` и не пробрасывайте порты 17321–17323 напрямую.
- Токены лежат только локально в `%USERPROFILE%\.chatgpt-desktop-agent`.
- Физический ввод отключён по умолчанию; его включение должно быть явным для
  конкретного вызова.
- Browser Bridge обладает правами текущего браузерного профиля. Считайте его
  токен таким же чувствительным, как активную сессию браузера.
- Revit‑мост умеет компилировать и выполнять C# внутри Revit. Устанавливайте его
  только на доверенном ПК и не отдавайте порт/токен внешним процессам.
- Remote Commander — внешняя зависимость. Репозиторий не содержит его исходники,
  учётные данные или `device.json`.

Подробности: [модель угроз](docs/SECURITY_RU.md) и [SECURITY.md](SECURITY.md).

## Структура

```text
src/desktop_agent/   локальный роутер, UIA/COM и MCP proxy
src/browser_bridge/  Playwright HTTP/stdio bridge
src/revit_bridge/    исходник необязательного Revit add-in
scripts/             установка, диагностика, запуск и удаление
tools/               фасад rdc_web с журналом и commit-gate
examples/            безопасные read-only payload-примеры
docs/                архитектура, эксплуатация и устранение проблем
tests/               static security/regression checks
```

## Документация

- [Установка и передача коллеге](docs/INSTALL_RU.md)
- [Архитектура и маршрутизация](docs/ARCHITECTURE_RU.md)
- [Remote Desktop Commander](docs/REMOTE_COMMANDER_RU.md)
- [Подключение MCP-клиента](docs/MCP_CLIENT_RU.md)
- [Browser Bridge](docs/BROWSER_BRIDGE_RU.md)
- [Revit Bridge](docs/REVIT_RU.md)
- [Эксплуатация](docs/OPERATIONS_RU.md)
- [Навигатор обновлений](docs/UPDATES_RU.md)
- [Устранение проблем](docs/TROUBLESHOOTING_RU.md)
## Известные границы

- Экран входа, UAC secure desktop и заблокированная Windows‑сессия недоступны
  обычному пользовательскому процессу.
- Приложение, запущенное с повышенными правами, может не принимать UIA/Win32
  команды от не elevated‑агента.
- Custom/GPU‑интерфейсы иногда требуют native API или визуального fallback.
- Автоматизация браузера не отменяет подтверждения перед платежом, публикацией,
  отправкой сообщения или другим внешним действием.
- Поддержка Revit 2025+ пока отсутствует.

## Разработка

```powershell
python -m compileall -q src tools tests
node --check src\browser_bridge\bridge.js
node --check src\desktop_agent\mcp\server.mjs
python -m unittest discover -s tests -v
```

В CI дополнительно проверяется синтаксис PowerShell, отсутствие runtime‑секретов,
локальных путей и опасной сетевой привязки.

## Лицензия и зависимости

Собственный код проекта распространяется по MIT. Remote Desktop Commander,
Playwright, MCP SDK, Autodesk Revit API и остальные зависимости имеют свои
лицензии и устанавливаются отдельно. См. [THIRD_PARTY.md](THIRD_PARTY.md).
