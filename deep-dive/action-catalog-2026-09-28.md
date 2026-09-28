# Deep dive: action catalog and self-describing harness (v0.2.0)

Mode: compact · Level: mid · Scope: changes of 2026-09-28

## Overview

The harness is only as useful as the model's picture of it. Before this change the
model saw 17 of ~50 actions, no parameters and adapters as bare file names, so it
guessed. Now one table (`catalog.py`) describes every action; the dispatcher,
`capabilities` and `describe` are all derived from it, and adapters describe
themselves through a manifest. Errors say how to recover.

## Key components

- `src/desktop_agent/catalog.py`: pure-Python table `ACTIONS` / `BROWSER_ACTIONS`, side-effect rules, `capabilities()`, `describe_*()`, `suggest()`. No Windows imports, so tests and `rdc_web.py` can load it anywhere.
- `agent.py::build_handlers` / `handle`: name → function table; `handle` checks the catalog, required params and the `physical` effect *before* running anything.
- `agent.py::do_browser`: server-side commit gate plus audit log for browser clicks and Enter presses.
- `agent.py::error_payload` + `Handler.do_POST`: structured errors mapped to 400 (fix the request), 403 (needs explicit permission), 500 (target failed).
- `core_ext.py::load_adapter` / `adapter_manifest` / `install_adapter`: mtime cache, `DESCRIPTION` + `ACTIONS` contract, rollback on a bad install.
- `core_ext.py::is_protected`: denylist for `file_handoff` (tokens, device identity, browser profile).
- `mcp/server.mjs::timeoutFor`: the proxy waits as long as the action's own timeout.
- `bridge.js::run`: storage state is saved only after actions that can change it.

## Concepts

**Single source of truth (registry pattern).**
*What:* one data table describes the capabilities, and code is keyed off it.
*Why:* the old `capabilities` was a hand-written list that had already drifted from the `if action == ...` chain. A table plus a test that compares handlers with the catalog, and the catalog with `bridge.js`, makes drift a CI failure.
*Trade-off:* the metadata sits in a separate file from the implementation. Decorators on each handler would keep them together, but they would have required importing the Windows-only modules to read the catalog.

**Self-describing API for LLM clients.**
*What:* the tool exposes progressive disclosure: `capabilities` gives a compact map, `describe` gives detail on demand.
*Why:* dumping all parameter schemas into the MCP tool description would cost context on every turn. Two-step discovery keeps the first call cheap.
*Alternative:* one MCP tool per action with a strict zod schema. That is more precise but adds ~50 tools, and adapters loaded at runtime could not have static schemas.

**Enforce policy at the boundary, not in the client.**
*What:* the commit gate and the physical-input check live in the agent, which every channel passes through.
*Why:* the gate used to live only in the `rdc_web.py` CLI, so MCP skipped it. Checks that run in the client are advisory.
*Limit:* the gate is a keyword heuristic on locator text. It lowers the risk but does not replace user confirmation.

**Fail-closed installs.**
`adapter_install` writes the file, loads it, validates the manifest and restores the previous file if any step fails. Without this, a bad upload would break an adapter that was working.

**Error design for machines.**
An error message is also input for the model's next attempt. `did_you_mean` (difflib), `valid_actions` and `params` turn one failed call into one corrected call rather than a guessing loop. HTTP status tells the model whether retrying with different arguments makes sense (400) or whether it has to ask the user (403).

## Verification

- `python -m pyflakes src tools tests`: clean. It would have caught the `win32process` bug on its own.
- `python -m unittest discover -s tests`: 37 tests. `test_agent_runtime.py` starts a real agent on a free port with temporary tokens.
- Manual smoke run: MCP client over stdio against the proxy (capabilities, describe, error hint, a 200 s `exec` timeout path); bridge status/open/snapshot/restart in headless mode with a temporary profile.
