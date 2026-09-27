# RDC Web Harness

Purpose: let ChatGPT drive web tasks through Remote Desktop Commander without relying on screen coordinates.

## Route

RDC -> `rdc-web` -> desktop-agent (:17322) -> Playwright bridge (:17321) -> web.

The browser profile is persistent at:
`%USERPROFILE%\.playwright-chatgpt`

Actions are logged to:
`%USERPROFILE%\desktop-agent\logs\browser-actions.ndjson`

## Default policy

Use DOM / ARIA locators first. Prefer `role`, `label`, `placeholder`, `testid`, then CSS selectors.
Do not use physical mouse/keyboard for web unless Playwright cannot do the job.
Navigation, reading, filling forms, uploads, downloads and screenshots may run normally.

Externally visible side effects such as Send/Post/Publish/Submit/Delete/Buy/Pay/Book require an explicit committed click:
`rdc-web --commit`

Without `--commit`, suspicious side-effect clicks are blocked before Playwright receives them.

## Examples

```powershell
'{"action":"open","url":"https://example.com"}' | rdc-web
'{"action":"snapshot"}' | rdc-web
'{"action":"fill","label":"Message","value":"Hello"}' | rdc-web
'{"action":"click","role":"button","name":"Send"}' | rdc-web --commit
```

For authenticated sites, use the persistent Playwright profile. If login is missing, restart the bridge headed, sign in once, and the saved storage state can be reused.
