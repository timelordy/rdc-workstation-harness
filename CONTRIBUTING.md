# Contributing

## Principles

Keep the harness local, explicit and inspectable. Prefer a stable application
interface over screen coordinates. New features must not weaken the default
loopback-only boundary or enable physical input by default.

## Once per clone

```powershell
git config core.hooksPath .githooks
```

The pre-commit hook runs gitleaks on staged changes, rejects anything staged
from `local/` and checks your private stop-list. Keep personal uploaders, cloud
credentials and stop-words outside Git: see `docs/LOCAL_PRIVATE_RU.md`.

## Before opening a pull request

```powershell
python -m compileall -q src tools tests
node --check src\browser_bridge\bridge.js
node --check src\desktop_agent\mcp\server.mjs
node --check src\desktop_agent\mcp\task-tools.mjs
npm ci --prefix src/desktop_agent/mcp --ignore-scripts --no-audit --no-fund
npm ci --prefix src/browser_bridge --ignore-scripts --no-audit --no-fund
node --test tests/share_hook.test.mjs tests/control_tools.test.mjs tests/task_tools.test.mjs tests/runtime_version.test.mjs tests/browser_bridge.test.mjs tests/cdp_attach.test.mjs
python -m unittest discover -s tests -v
```

Browser behaviour also has an isolated loopback fixture test. CI requires it;
locally, install Chromium and opt in before running it:

```powershell
Push-Location src/browser_bridge
npx --no-install playwright install chromium
Pop-Location
$env:RDC_BROWSER_INTEGRATION = "1"
node --test tests/browser_bridge.integration.test.mjs tests/cdp_attach.integration.test.mjs
```

The fixture uses a temporary browser profile and no signed-in accounts.

Parse all PowerShell scripts as CI does and run `scripts\diagnose.ps1` on a
Windows test account. Do not test against production projects or personal
browser profiles.

## Pull request checklist

- No local absolute paths, credentials, cookies, device identities or logs.
- New ports bind to loopback only.
- Side effects and physical input require an explicit caller decision.
- Install/update/uninstall remain idempotent.
- Documentation covers setup, rollback and known limitations.
- Dependencies are pinned and their lockfiles are updated.
- App-specific behavior lives in an adapter when practical.

## Adapter design

An adapter should be small, public-interface based and removable without
changing the core router. Good interfaces include vendor SDKs, COM, CDP, CLI,
RPC and documented local protocols. UIA is a fallback; physical input is the
last fallback.
