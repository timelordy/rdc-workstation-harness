# Changelog

All notable changes are documented here.

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
