# Browser Bridge

## Зачем отдельный мост

Web-интерфейсы надёжнее управлять через DOM/ARIA и Playwright, а не координатами
экрана. Это меньше зависит от масштаба, положения окна и движения реальной мыши.
Bridge работает с отдельным persistent-профилем и слушает только loopback.

## Запуск и health

```powershell
Invoke-RestMethod http://127.0.0.1:17321/health
```

Privileged actions идут через `POST /action` с локальным browser token. Обычно
к ним обращается Desktop Agent, поэтому вручную читать или передавать token не
нужно.

## Выбор браузера

В `auto` проверяются Brave, Chrome и Edge в per-user и стандартных
`Program Files` каталогах. Если системный браузер не найден, установщик
автоматически ставит Chromium Playwright.

Выбор задаётся установщиком или переменными `PW_EXECUTABLE`/`PW_CHANNEL`.
Используется отдельный каталог `PW_PROFILE_DIR`, а не основной профиль браузера.
## Headed и headless

Для ручного входа в сайт переключите Bridge в видимый режим:

```powershell
.\scripts\set-browser-mode.ps1 -Mode headed
```

После входа верните фон:

```powershell
.\scripts\set-browser-mode.ps1 -Mode headless
```

Скрипт меняет runtime-config и перезапускает только Browser Bridge.

## Locator policy

Предпочтительный порядок:

1. `role` + доступное имя;
2. `label`;
3. `placeholder`;
4. `testid`;
5. точный текст;
6. CSS selector как последний устойчивый вариант.

Не используйте screen coordinates для web, пока DOM доступен. Перед кликом
проверяйте URL, title и нужный элемент; после клика — ожидаемое состояние.
## Поддерживаемые операции

Bridge поддерживает навигацию, snapshot, locator click/fill/type/press, ожидание,
чтение текста/HTML/URL, screenshots, upload/download, tabs, console/errors,
back/forward/reload, cookies/storage и ограниченный `evaluate`.

Пример payload через фасад:

```powershell
python .\tools\rdc_web.py '{"action":"goto","url":"https://example.com"}'
python .\tools\rdc_web.py '{"action":"text","role":"heading","name":"Example Domain"}'
```

Клик, похожий на внешнее действие, без явного commit блокируется:

```powershell
python .\tools\rdc_web.py `
  '{"action":"click","role":"button","name":"Submit"}'

python .\tools\rdc_web.py --commit `
  '{"action":"click","role":"button","name":"Submit"}'
```

`--commit` означает только разрешение фасада. Он не доказывает корректность
действия и не заменяет проверку формы, адресата, суммы или итогового состояния.
## Локальные данные

- profile: `%LOCALAPPDATA%\RdcWorkstationHarness\browser-profile`;
- downloads/screenshots: `%USERPROFILE%\Downloads\rdc-harness-browser`;
- log: `%LOCALAPPDATA%\RdcWorkstationHarness\logs\browser-bridge.log`;
- token: `%USERPROFILE%\.chatgpt-desktop-agent\browser.token`.

Обычный update не удаляет профиль. Обычный uninstall тоже сохраняет его, если не
указан `-RemoveBrowserProfile`.

## Ограничения безопасности

- `evaluate`, cookies и storage доступны только доверенному локальному caller.
- Upload должен использовать известный локальный файл, а не произвольный путь
  из недоверенного web-контента.
- Download требует проверки имени, MIME и места сохранения.
- Не держите в профиле аккаунты, не нужные для задач harness.
- Не пробрасывайте порт 17321 через Tailscale, SSH tunnel или reverse proxy.
- Перед платежом, отправкой сообщения, публикацией и удалением нужен отдельный
  уровень подтверждения вызывающей стороны.
