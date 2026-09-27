# Эксплуатация

## Ежедневные команды

```powershell
# Проверить стек
.\scripts\diagnose.ps1

# Запустить core
.\scripts\start.ps1

# Запустить core и Remote Commander
.\scripts\start.ps1 -IncludeRemoteCommander

# Остановить всё
.\scripts\stop.ps1 -IncludeRemoteCommander
```

Health endpoints:

```text
http://127.0.0.1:17321/health  Browser Bridge
http://127.0.0.1:17322/health  Desktop Agent
```

## Logs

```text
%LOCALAPPDATA%\RdcWorkstationHarness\logs\desktop-agent.log
%LOCALAPPDATA%\RdcWorkstationHarness\logs\browser-bridge.log
%LOCALAPPDATA%\RdcWorkstationHarness\logs\browser-actions.ndjson
```

Логи не ротируются централизованно. На долгоживущем ПК их стоит периодически
архивировать или удалять после проверки, не отправляя в общий репозиторий.
## Обновление

```powershell
git pull
.\scripts\install.ps1 -InstallRemoteCommander
.\scripts\diagnose.ps1
```

Перед обновлением с изменением major-версий зависимостей сохраните текущий commit:

```powershell
git rev-parse HEAD
```

Rollback:

```powershell
git checkout <previous-commit>
.\scripts\install.ps1 -InstallRemoteCommander
```

Runtime-код заменяется из checkout, но токены и браузерный профиль сохраняются.
Если проблема связана именно с state/profile, сохраните его копию и выполните
отдельную очистку.

## Смена браузерного режима

```powershell
.\scripts\set-browser-mode.ps1 -Mode headed
.\scripts\set-browser-mode.ps1 -Mode headless
```

Видимый режим нужен для первого входа, CAPTCHA, MFA или диагностики. Для обычной
работы предпочтителен headless, если конкретный сайт его не блокирует.
## Ротация credentials

```powershell
.\scripts\stop.ps1 -IncludeRemoteCommander
.\scripts\install.ps1 -InstallRemoteCommander -RotateTokens -SkipStart
.\scripts\start.ps1 -IncludeRemoteCommander
.\scripts\diagnose.ps1
```

Remote Commander identity меняется не этим ключом. Для неё используется
удаление identity и новое pairing.

## Проверка автозапуска

Нормальное состояние — максимум одна задача каждого типа:

```powershell
Get-ScheduledTask | Where-Object TaskName -like "RDC Harness*"
```

Legacy Startup `.vbs` и задачи со старыми именами удаляйте только после того,
как новый runtime прошёл health checks. Иначе можно потерять рабочий канал.

## Резервирование

Исходники резервируются Git. Runtime целиком переносить не нужно. При желании
сохранить браузерную сессию делайте локальную зашифрованную копию profile, но
не передавайте её коллеге. Tokens и device identity также не являются частью
backup репозитория.

## Плановая проверка

После обновления Windows, Python, Node.js, браузера или Playwright:

1. запустите `diagnose.ps1`;
2. проверьте простой read-only сценарий;
3. проверьте один locator в Browser Bridge;
4. только затем разрешайте внешние действия.
