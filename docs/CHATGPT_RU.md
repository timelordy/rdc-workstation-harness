# ChatGPT (веб) через Remote Desktop Commander

ChatGPT в браузере не запускает локальный MCP proxy harness. Он подключён к
облачному Remote Desktop Commander, а тот передаёт команды агенту RDC на ПК.
Поэтому ChatGPT работает с harness через терминал (`pc-agent`) и файлы.

```text
ChatGPT (web) -> mcp.desktopcommander.app -> RDC на ПК (+ share hook)
    start_process: pc-agent ...   -> Desktop Agent :17322
    read_file: <картинка/документ> -> текст со ссылкой на файл
```

## pc-agent

Установщик кладёт `pc-agent` в `%LOCALAPPDATA%\RdcWorkstationHarness\tools` и
добавляет эту папку в пользовательский PATH. После установки перезапустите
Remote Commander, чтобы он увидел новый PATH.

```text
pc-agent health
pc-agent capabilities
pc-agent describe uia_click
pc-agent look                                  весь экран
pc-agent look --window "AutoCAD"               окно по регулярке заголовка
pc-agent look --hwnd 133850 --max-side 1024
pc-agent look --region 0,0,800,600
pc-agent "{\"action\": \"windows\", \"title_re\": \"Excel\"}"
pc-agent @request.json
```

Вывод — компактный JSON только из ASCII-символов. Base64 в терминал не попадает
никогда: `look` сохраняет JPEG в `%USERPROFILE%\Downloads\rdc-harness\chat\`
(хранятся последние 50 снимков) и печатает путь. Если в имени профиля есть
кириллица, путь выдаётся в коротком 8.3-виде (например, `%USERPROFILE%` вида
`...\2AEA~1\...`): консоль PowerShell 5.1 искажает кириллицу, и `read_file`
не нашёл бы файл.

## Показать скриншот в чате

1. `start_process`: `pc-agent look --window "AutoCAD"`
2. Из ответа взять `result.path`.
3. `read_file` с этим путём — RDC вернёт картинку.

**Проверено на практике:** ChatGPT (веб) сейчас теряет картинки из `read_file`,
пришедшие через облачный Remote Desktop Commander: вызов успешен, модель пишет
«скриншот выше», но в чате ничего нет. Это ограничение ChatGPT, а не ПК
(см. обсуждения на community.openai.com про пустой `{}` вместо image block).
Для ChatGPT поэтому нужна ссылка — см. следующий раздел.

## Ссылка на снимок или файл (`--share`)

Harness сам никуда не загружает. `pc-agent look --share` и `pc-agent share <файл>`
передают файл команде, которую вы задаёте в переменной окружения
`RDC_HARNESS_SHARE_CMD`, и возвращают напечатанную ею ссылку:

```text
pc-agent look --window "AutoCAD" --share
  -> {"result": {"path": "...", "url": "https://..."}, "next": "give the user result.url"}
pc-agent share C:\path\report.pdf
  -> {"ok": true, "url": "https://..."}
```

Контракт команды: путь к файлу — последний аргумент; ссылка — последняя строка
stdout; ненулевой код возврата — ошибка. Это может быть загрузчик в ваше облако,
на ваш сервер или внутренний файлообменник. Пример:

```powershell
[Environment]::SetEnvironmentVariable("RDC_HARNESS_SHARE_CMD",
  'powershell -NoProfile -ExecutionPolicy Bypass -File C:\tools\my-upload.ps1', "User")
```

`pc-agent` читает переменную и из реестра пользователя, так что перезапускать
Remote Commander после её установки не нужно. Помните, что ссылка может
открываться у любого, кто её получит: выбирайте хранилище и срок жизни ссылок
сами. Сам загрузчик и доступы к облаку держите вне репозитория — см.
[личные данные и репозиторий](LOCAL_PRIVATE_RU.md).

Пиксель картинки переводится в экранные координаты по полям ответа:
`screen_x = screen_rect.left + image_x / scale`.

## Нативный путь без инструкций: share hook

ChatGPT видит только фиксированный список инструментов облачного RDC
(`read_file`, `start_process`, ...). Список собирается в облаке; устройство
передаёт туда лишь версию, так что свой инструмент или описание добавить нельзя.
Без инструкций ChatGPT делает скриншот своим способом и открывает его
`read_file` — это и есть точка, где harness может помочь.

`tools/rdc_share_hook.mjs` загружается в агент RDC на ПК (`node --import ...
index.js remote`; установщик с `-InstallRemoteCommander` делает это сам) и
дополняет ответ удалённого `read_file`:

- для картинок и документов (`png/jpg/gif/webp/bmp`, `pdf/docx/xlsx/pptx/dwg/...`)
  файл публикуется через `RDC_HARNESS_SHARE_CMD`, а в ответ добавляется текст
  со ссылкой, который модель передаёт пользователю;
- image block удаляется для всех клиентов, кроме Claude: ChatGPT превращает
  ответ с картинкой в пустой `{}`, и ссылка пропала бы вместе с ней;
- без `RDC_HARNESS_SHARE_CMD`, для текстовых файлов и других инструментов ответ
  не меняется; при любой ошибке хука возвращается исходный ответ;
- один и тот же файл публикуется один раз; события пишутся в
  `%USERPROFILE%\.rdc-workstation-harness\logs\share-hook.ndjson`.

Хук меняет поведение пакета Remote Desktop Commander, не редактируя его файлы,
через патч `DesktopCommanderIntegration.callClientTool`. Если пакет
переименует этот метод, хук запишет `hook_not_installed` в журнал и ничего не
сломает. Публикация добавляет несколько секунд к `read_file` картинки.

## Инструкция для ChatGPT (необязательно)

С share hook ссылки приходят и без инструкций. Инструкция ускоряет работу
(`pc-agent look` снимает окно в фоне) и открывает остальные возможности harness.
Вставьте в инструкции проекта ChatGPT или в custom instructions:

```text
На моём ПК через Desktop Commander доступен harness `pc-agent`.
- Начинай с `pc-agent capabilities`; параметры действия — `pc-agent describe <имя>`.
- Чтобы показать мне экран или окно: `pc-agent look --share` (или
  `--window "<заголовок>" --share`) и пришли мне ссылку из result.url.
  Картинки из read_file в этом чате не отображаются — не пиши «скриншот выше».
- Чтобы отдать мне файл: `pc-agent share "<путь>"` и пришли ссылку.
- Не выводи картинки и base64 через терминал.
- Предпочитай адаптеры, COM и UIA; физические мышь и клавиатура — только по моей просьбе.
- Клики «отправить / оплатить / удалить» в браузере требуют моего подтверждения,
  после него повтори запрос с "commit": true.
```

## Файлы

`read_file` в RDC отдаёт содержимое текстом (PDF — страницами, Excel —
таблицей). Скачать сам файл из ответа MCP-инструмента ChatGPT не позволяет;
используйте `pc-agent share`.

## Ограничения

- `read_file` в RDC читает любые файлы, если в его настройке пустой
  `allowedDirectories`. Защита harness от выдачи токенов действует только для
  `file_handoff`; ограничьте RDC нужными папками, если это важно.
