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

Locator должен найти ровно один элемент. Раньше bridge без предупреждения брал
первый результат; теперь неоднозначный selector даёт ошибку. Если порядок
элементов действительно проверен, передайте `nth` (индекс с нуля). Предпочитайте
уникальные label/role/name вместо позиционного выбора.

## Вкладки и вложенные формы

`tabs` возвращает `tab_id` и прежний `index`. Передавайте `tab_id` с каждой
командой: соседние вкладки могут закрыться и изменить индексы. ID действует
только пока жива соответствующая вкладка и процесс bridge. Закрытый ID даёт
ошибку; bridge не подменяет его текущей вкладкой. `useTab` и `closeTab` сохраняют
поддержку старого `index` для существующих клиентов.

`frames` перечисляет frame выбранной вкладки: `frame_id`, `parent_frame_id`,
name, URL и признак основного frame. Для locator actions и snapshot можно
передать `frame_id` или `frame_selector`. Вложенность задаётся массивом:
`["#outer-frame", "#inner-frame"]`. После замены/отсоединения frame его ID надо
получить заново.

Вызовы HTTP и stdio проходят через одну очередь вместе с сохранением состояния.
Это предотвращает гонку запуска browser context и перемешивание команд.
Несколько отдельных вызовов всё ещё не являются одной атомарной транзакцией:
явно задавайте вкладку и проверяйте результат.

Не начатый запрос ждёт очереди не более 30 секунд. При истечении срока bridge
возвращает `QUEUE_TIMEOUT` с `action_started: false` и не выполняет команду
позднее. Разрыв HTTP соединения также отменяет только не начатую команду.
Уже запущенный browser operation это не прерывает.

Если действие выполнено, но запись storage state завершилась ошибкой, ответ
сохраняет результат и добавляет `action_completed: true`, `state_saved: false`
и `state_warning`. Повторять само действие ради повторного сохранения нельзя.
## Поддерживаемые операции

Bridge поддерживает навигацию, snapshot, locator click/fill/type/press, ожидание,
чтение текста/HTML/URL, screenshots, upload/download, tabs, console/errors,
back/forward/reload, cookies/storage и `evaluate` для доверенного caller.

Новые операции для обычных форм:

| Action | Параметры | Результат |
|---|---|---|
| `inspect` | locator, `max_length` (default 500), `max_options` (default 50) | Состояние поля, значение, selected options, checked/readonly/disabled/required |
| `selectOption` | locator, `value`: строка или массив строк | Выбранные значения native select |
| `setChecked` | locator, `checked`: boolean | Проверенное состояние checkbox/radio |
| `hover` | locator | Наведение DOM-курсора |
| `scrollIntoView` | locator | Элемент прокручен в видимую область |
| `frames` | `tab_id`, `max_frames` (default 100) | Ограниченный список live frames |

`inspect` возвращает `element.name` как подсказку по label/ARIA, а не полный
расчёт W3C accessible name. `snapshot`/Playwright role locators остаются основным
способом семантического поиска. Парольное `value` скрыто; select options и текст
ограничены. Custom dropdown заполняется через его ARIA controls; `selectOption`
предназначен для native `<select>`.

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

Существующий commit-gate — эвристика по названию клика/клавише; он не является
границей полномочий или доказательством отсутствия побочных эффектов. JS
обработчики могут сохранять данные при изменении обычного поля. Caller должен
действовать в пределах запроса пользователя и проверять итог внешнего действия.
