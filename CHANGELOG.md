# Changelog

All notable changes are documented here.

## [0.2.1] - 2026-09-28

### Added

- `pc-agent look --share` and `pc-agent share <file>`: hand a screenshot or file to a user-configured uploader (`RDC_HARNESS_SHARE_CMD`) and return its URL. ChatGPT web drops images returned by `read_file` through Remote Desktop Commander, so a link is the working delivery path. Nothing is uploaded unless the variable is set.

### Changed

- `docs/CHATGPT_RU.md` documents that `read_file` images do not reach ChatGPT web and updates the suggested ChatGPT instructions to use links.

## [0.2.0] - 2026-09-28

### Added

- Action catalog (`catalog.py`): one description of every action drives the dispatcher, `capabilities` and the new `describe` action. `capabilities` now lists all ~50 actions (previously 17), browser actions and adapters.
- MCP tool `desktop_describe` and routing guidance in the MCP tool descriptions.
- MCP tool `desktop_look` / action `look`: the model sees a window, region or monitor as an image (downscaled JPEG) with a pixel-to-screen mapping. Windows are captured in the background; blank GPU captures fall back to visible screen pixels.
- `pc-agent` terminal CLI (installed on the user PATH) for ChatGPT via Remote Desktop Commander. `pc-agent look` saves the JPEG under `Downloads\rdc-harness\chat` (last 50 kept) and prints the path for `read_file`; base64 never reaches the terminal. See `docs/CHATGPT_RU.md`.
- Adapter manifest contract (`DESCRIPTION`, `ACTIONS`); `adapter_list` shows what each adapter does; `adapter_call` rejects unknown adapter actions with the valid list.
- Structured errors with `hint`, `did_you_mean`, `valid_actions` or `params`; HTTP 400/403/500 separate request errors, missing permission and target failures.
- `com_list` action; `com_release` reports whether a handle existed.
- Behaviour tests that run a real agent on a free port; catalog/bridge consistency tests; pyflakes in CI.

### Fixed

- `probe_target` always returned an empty `windows` list (missing `win32process` import was swallowed).
- `com_set` without `path` crashed with `AttributeError`.
- MCP proxy cut off long `exec`/`python`/`powershell`/browser calls after 135 s while they kept running; it now waits for the action's own timeout.
- Browser `restart` without `headed` silently switched to headless.
- Adapters were re-imported as new modules on every change and never released; they are now cached by mtime.
- Bridge errors reached the model as a bare `HTTP Error 500`; the bridge's message is now forwarded.

### Security

- The browser side-effect gate is enforced by the agent for every channel (MCP, CLI, HTTP) and also covers `Enter` presses; `rdc_web.py` shares the same rules. Previously MCP bypassed it.
- `file_handoff` refuses tokens, device identity and browser-profile files.
- Browser storage state is written only after actions that can change it.
- `adapter_install` rolls back to the previous file if the new adapter fails to load.

## [0.1.1] - 2026-09-27

### Removed

- Revit Bridge source, adapter, installation scripts, examples and active documentation.
- Revit Bridge configuration and checks from install, update, diagnose and uninstall scripts. The installer still accepts the old port argument so the v0.1.0 updater can complete this upgrade.

## [0.1.0] - 2026-09-27

### Added

- Public, sanitized workstation harness extracted from a working Windows setup.
- Loopback Desktop Agent with UIA, Win32, COM, adapters and MCP proxy.
- Loopback Playwright Browser Bridge with automatic Brave/Chrome/Edge detection.
- Optional Remote Desktop Commander installation and per-device pairing flow.
- Optional Revit 2022-2024 bridge built locally against installed Revit APIs.
- Idempotent PowerShell install, update, start, stop, diagnose and uninstall tools.
- One scheduled task per component instead of duplicate Startup entries.
- Russian handoff, architecture, security, operations and troubleshooting docs.
- Static checks for secrets, local paths, loopback binding and generated files.
- Reproducible Windows CI, read-only examples and MCP client handoff documentation.
- Versioned update navigator with stable/main channels, daily checks, one-time Windows notifications, backup-aware updater and install-state tracking.
- Browser discovery across per-user and Program Files locations with bundled fallback.
- Russian and English browser side-effect hints in the commit gate.
- Windows CI for Python, Node.js and PowerShell syntax.
- Update rollback covers installed dependencies and restarts prior services; archive URLs are restricted to the configured GitHub repository.
- Installer stops on failed dependency commands; Windows browser discovery handles missing Program Files environment variables.

### Changed

- Removed unused Playwright CLI/MCP packages from the browser bridge runtime.
- Replaced hard-coded workstation paths and browser choice with environment-based configuration.
- Made browser and desktop watchdog ports/log paths configurable.
- Kept physical mouse and keyboard fallback disabled by default.

### Not included

- Local tokens, browser state, screenshots, logs, output files or device identity.
- Prebuilt Autodesk DLLs.
- Codex remote-control keepalive based on non-public/unstable app-server behavior.
