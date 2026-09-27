# Remote Desktop Commander в составе harness

## Роль

Remote Desktop Commander Remote отвечает за транспорт до конкретного Windows-ПК
и базовые операции с файлами, терминалом и процессами. Desktop Agent и Browser
Bridge дополняют его UIA/COM/Playwright-возможностями, но не заменяют pairing RDC.

Пакет `@wonderwhy-er/desktop-commander` не копируется в Git. Установщик получает
закреплённую версию из npm в локальный runtime-каталог.

## Установка

```powershell
.\scripts\install.ps1 -InstallRemoteCommander
```

По умолчанию используется закреплённая версия 0.2.50. Другую
версию можно указать явно:

```powershell
.\scripts\install.ps1 `
  -InstallRemoteCommander `
  -RemoteCommanderVersion 0.2.50
```

Не используйте `latest` для воспроизводимой установки коллеги: новая версия
может поменять pairing, CLI или runtime-поведение.
## Первое сопряжение

```powershell
.\scripts\pair-remote-commander.ps1
```

Скрипт запускает установленный CLI в видимом терминале. Выполните предлагаемые
шаги входа/подтверждения. После успешного pairing появляется локальная identity:

```text
%USERPROFILE%\.desktop-commander-device\device.json
```

Это не переносимая конфигурация проекта. На другом ПК запускается новое
сопряжение. Не добавляйте identity в архив, Git, OneDrive или общий сетевой диск.

## Автозапуск

После pairing задача `RDC Harness - Remote Commander` запускает один экземпляр
при входе текущего пользователя. Установщик перезаписывает задачу с тем же
именем, поэтому обновление не создаёт дубли.

Проверка:

```powershell
Get-ScheduledTask -TaskName "RDC Harness - Remote Commander"
.\scripts\diagnose.ps1
```

Ручной запуск/остановка:

```powershell
.\scripts\start.ps1 -IncludeRemoteCommander
.\scripts\stop.ps1 -IncludeRemoteCommander
```
## Дубли старой установки

Ранее harness мог запускаться одновременно через Startup `.vbs`, глобальный
`npx` и несколько Scheduled Tasks. Новый репозиторий эти legacy-записи сам не
удаляет, чтобы не остановить чужую конфигурацию без разрешения. `diagnose.ps1`
показывает подозрительные задачи.

После проверки можно вручную отключить старые записи и оставить только:

```text
RDC Harness - Desktop Agent
RDC Harness - Browser Bridge
RDC Harness - Remote Commander
```

Сначала убедитесь, что новые health checks проходят и новый RDC device виден в
клиенте. Только затем удаляйте legacy-autostart.

## Обновление

Версия package задаётся `-RemoteCommanderVersion`. При смене версии установщик
обновляет локальный `package.json` и зависимости runtime. Identity сохраняется.
После обновления выполните pairing заново только если сам RDC-клиент сообщает,
что текущая регистрация больше недействительна.

## Полное удаление identity

Обычный uninstall сохраняет identity. Для отвязки именно этого ПК:

```powershell
.\scripts\uninstall.ps1 -RemoveRemoteCommanderIdentity
```

После удаления следующее использование требует нового pairing.
