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
node --test tests/share_hook.test.mjs
python -m unittest discover -s tests -v
```

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
