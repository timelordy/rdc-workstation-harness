# Установка и передача коллеге

## Что передаётся

Коллеге передаётся ссылка на репозиторий. Не архив рабочей папки и не снимок
пользовательского профиля. Репозиторий содержит только исходники, lock-файлы,
установщик, диагностику и документацию.

## Требования

- Windows 10/11 x64;
- Python 3.10+ в `PATH`;
- Node.js 18+ и `npm.cmd` в `PATH`;
- Git;
- доступ к npm/PyPI во время первой установки;
- Revit 2022–2024 только для необязательного Revit-моста.

Базовая установка выполняется от обычной интерактивной учётной записи Windows.
Ограничения корпоративного домена могут потребовать помощи администратора.

## Чистая установка

```powershell
git clone https://github.com/timelordy/rdc-workstation-harness.git
cd rdc-workstation-harness
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1 -InstallRemoteCommander
```
Runtime создаётся в `%LOCALAPPDATA%\RdcWorkstationHarness`. Для обычного
пользователя `git pull` после установки не нужен: updater сам скачивает новый
Release или `main` и повторно применяет installer. Рабочие токены и браузерный
профиль лежат вне Git.

Установщик создаёт:

- `.venv` с закреплёнными Python-зависимостями;
- отдельные каталоги Desktop Agent и Browser Bridge;
- локальные токены в `%USERPROFILE%\.chatgpt-desktop-agent`;
- по одной задаче Windows для каждого выбранного компонента;
- отдельный браузерный профиль и журналы runtime.

## Полезные параметры

```powershell
# Установить без Remote Commander
.\scripts\install.ps1

# Использовать установленный Chrome
.\scripts\install.ps1 -Browser chrome

# Поставить Chromium Playwright
.\scripts\install.ps1 -Browser bundled -InstallBundledChromium

# Не создавать автозапуск core-служб
.\scripts\install.ps1 -NoAutostart

# Получать каждый push из main вместо Releases
.\scripts\install.ps1 -UpdateChannel main

# Полностью отключить фоновую проверку обновлений
.\scripts\install.ps1 -NoUpdateCheck
```
```powershell
# Установить, но пока не запускать службы
.\scripts\install.ps1 -SkipStart

# Перевыпустить локальные токены
.\scripts\install.ps1 -RotateTokens

# Другой каталог runtime
.\scripts\install.ps1 -InstallRoot D:\Tools\RdcWorkstationHarness
```

Повторный запуск с теми же параметрами является обновлением: код и зависимости
обновляются, задачи перерегистрируются, существующие токены сохраняются.

## Одноразовое сопряжение Remote Commander

После первой установки выполните в видимом окне PowerShell:

```powershell
.\scripts\pair-remote-commander.ps1
```

Завершите вход или подтверждение, которое покажет программа. После появления
локальной идентичности компонент можно запускать фоном:

```powershell
.\scripts\start.ps1 -IncludeRemoteCommander
```
Идентичность создаётся отдельно на каждом ПК. Каталог
`%USERPROFILE%\.desktop-commander-device` не входит в репозиторий и не должен
передаваться вместе с handoff.

## Подключение MCP-клиента

Установщик разворачивает stdio-server, но не переписывает конфигурацию вашего
AI-клиента. Получите точные локальные параметры:

```powershell
.\scripts\show-mcp-config.ps1
```

Добавьте выведенные `command` и `args` как MCP server `desktop_agent`.
Подробности: [MCP_CLIENT_RU.md](MCP_CLIENT_RU.md).

## Первый вход в сайты

Browser Bridge использует отдельный профиль. Для ручного входа включите окно:

```powershell
.\scripts\set-browser-mode.ps1 -Mode headed
```

После входа верните фоновый режим:

```powershell
.\scripts\set-browser-mode.ps1 -Mode headless
```
Основной профиль браузера автоматически не переносится. Коллега входит только
в нужные аккаунты в отдельном профиле harness.

## Проверка после установки

```powershell
.\scripts\diagnose.ps1
Invoke-RestMethod http://127.0.0.1:17322/health
Invoke-RestMethod http://127.0.0.1:17321/health
```

Затем выполните один безопасный пример из каталога `examples/`.
