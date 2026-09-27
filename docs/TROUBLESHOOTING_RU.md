# Устранение проблем

Начинайте не с переустановки всего мира, а с дешёвой диагностики:

```powershell
.\scripts\diagnose.ps1
Get-Content $env:LOCALAPPDATA\RdcWorkstationHarness\logs\desktop-agent.log -Tail 100
Get-Content $env:LOCALAPPDATA\RdcWorkstationHarness\logs\browser-bridge.log -Tail 100
```

## Desktop Agent не отвечает

Проверьте порт и процесс:

```powershell
Get-NetTCPConnection -LocalPort 17322 -State Listen
Get-CimInstance Win32_Process | Where-Object CommandLine -like "*RdcWorkstationHarness*agent.py*"
```

Если listener отсутствует, запустите:

```powershell
.\scripts\start.ps1
```

Если порт занят чужим процессом, определите PID и либо освободите порт, либо
переустановите harness на другой порт одинаково для installer и клиента.

## Browser Bridge healthy, но browser action падает

Health endpoint не запускает браузер. Первый action может выявить отсутствие
подходящего executable или Chromium. Переустановите с явным вариантом:

```powershell
.\scripts\install.ps1 -Browser edge
# или
.\scripts\install.ps1 -Browser bundled -InstallBundledChromium
```
Для входа, MFA или CAPTCHA временно включите окно:

```powershell
.\scripts\set-browser-mode.ps1 -Mode headed
```

После входа верните `headless`. Не копируйте основной профиль браузера в
runtime: это переносит лишние cookies, пароли и расширения вместе с проблемой.

## Remote Commander не видит ПК

Проверьте локальную identity и задачу:

```powershell
Test-Path $env:USERPROFILE\.desktop-commander-device\device.json
Get-ScheduledTask -TaskName "RDC Harness - Remote Commander"
```

Если identity отсутствует, выполните новое сопряжение:

```powershell
.\scripts\pair-remote-commander.ps1
```

Если процессов несколько, остановите legacy-задачи только после того, как новый
канал появился в клиенте. Одновременные `npx ... remote`, глобальная установка и
несколько Scheduled Tasks создают гонку, а не отказоустойчивость.

## UIA не управляет приложением

Сравните уровни прав. Обычный agent часто не может управлять приложением,
запущенным от администратора. Лучше запустить оба с одинаковыми обычными
правами, а не повышать весь harness без необходимости.

Для custom/GPU UI сначала ищите native API, COM, CLI или adapter. Координаты
мыши являются последним fallback и требуют явного `allow_physical=true`.
## Revit Bridge не загружается

Проверьте совпадение года Revit, manifest и DLL:

```powershell
Get-ChildItem $env:APPDATA\Autodesk\Revit\Addins -Filter RdcHarness.RevitBridge.addin -Recurse
.\scripts\install-revit-bridge.ps1 -RevitYear 2022
```

После установки полностью перезапустите Revit. Для 2025+ текущую сборку не
используйте: там другой runtime .NET, и оптимизм не является ABI-совместимостью.

## Чистый сброс runtime

Сначала сохраните нужные локальные результаты. Затем:

```powershell
.\scripts\uninstall.ps1 -Confirm:$false
.\scripts\install.ps1 -InstallRemoteCommander
.\scripts\diagnose.ps1
```

Обычный uninstall сохраняет browser profile, tokens и RDC identity. Полное
удаление выполняется только явными ключами `-RemoveBrowserProfile`,
`-RemoveTokens` и `-RemoveRemoteCommanderIdentity`.

## Что приложить к issue

Перед публикацией удалите секреты и корпоративные данные. Достаточный минимум:
версия Windows, Python/Node, commit репозитория, вывод `diagnose.ps1`, последние
50–100 строк нужного log и точный read-only payload, на котором воспроизводится
ошибка. Не прикладывайте token-файлы, `device.json`, browser profile или модели.
