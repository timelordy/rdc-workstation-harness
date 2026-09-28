# ChatGPT (веб) через Remote Desktop Commander

ChatGPT в браузере не запускает локальный MCP proxy harness. Он подключён к
облачному Remote Desktop Commander, а тот передаёт команды агенту RDC на ПК.
Поэтому ChatGPT работает с harness через терминал (`pc-agent`) и файлы.

```text
ChatGPT (web) -> mcp.desktopcommander.app -> RDC на ПК
    start_process: pc-agent ...   -> Desktop Agent :17322
    read_file: <путь к картинке>  -> картинка в чате
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
`...\D0A5~1\...`): консоль PowerShell 5.1 искажает кириллицу, и `read_file`
не нашёл бы файл.

## Показать скриншот в чате

1. `start_process`: `pc-agent look --window "AutoCAD"`
2. Из ответа взять `result.path`.
3. `read_file` с этим путём — RDC вернёт картинку, ChatGPT покажет её в чате.

Пиксель картинки переводится в экранные координаты по полям ответа:
`screen_x = screen_rect.left + image_x / scale`.

## Инструкция для ChatGPT

Вставьте в инструкции проекта ChatGPT или в custom instructions:

```text
На моём ПК через Desktop Commander доступен harness `pc-agent`.
- Начинай с `pc-agent capabilities`; параметры действия — `pc-agent describe <имя>`.
- Чтобы увидеть экран или окно: `pc-agent look` (или `--window "<заголовок>"`),
  затем read_file по пути из result.path. Не выводи картинки через терминал.
- Предпочитай адаптеры, COM и UIA; физические мышь и клавиатура — только по моей просьбе.
- Клики «отправить / оплатить / удалить» в браузере требуют моего подтверждения,
  после него повтори запрос с "commit": true.
```

## Файлы

`read_file` в RDC показывает содержимое: картинки — изображением, PDF — текстом
и страницами, Excel — таблицей. Скачать произвольный файл из ответа MCP-инструмента
ChatGPT сейчас не позволяет; для такого файла нужна ссылка (облачный диск).

## Ограничения

- ChatGPT может не показать картинку, если она очень большая; `look` по
  умолчанию делает JPEG до 1568 px, обычно 60–300 КБ. Уменьшайте `--max-side`,
  если изображение не появилось.
- `read_file` в RDC читает любые файлы, если в его настройке пустой
  `allowedDirectories`. Защита harness от выдачи токенов действует только для
  `file_handoff`; ограничьте RDC нужными папками, если это важно.
