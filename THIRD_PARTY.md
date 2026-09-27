# Third-party components

The repository contains original harness code and installation scripts. The
following components are installed as dependencies and retain their own
licenses and trademarks.

| Component | Role | Tested version | Distribution |
|---|---|---:|---|
| Remote Desktop Commander | remote device and filesystem/process connector | 0.2.50 | npm, MIT |
| Playwright | browser automation | 1.63.0 | npm, Apache-2.0 |
| Model Context Protocol SDK | MCP stdio proxy | 1.30.1 | npm, MIT |
| zod | MCP input validation | 4.6.5 | npm, MIT |
| PyAutoGUI | optional physical-input fallback | 0.9.54 | PyPI, BSD-3-Clause |
| pywinauto | Windows UI Automation | 0.6.9 | PyPI, BSD-3-Clause |
| pywin32 | Win32 and COM access | 311 | PyPI, PSF-2.0 |
| mss | screen capture | 10.1.0 | PyPI, MIT |
| Pillow | image handling | 11.3.0 | PyPI, HPND |
| psutil | process inspection | 7.0.0 | PyPI, BSD-3-Clause |
| pyperclip | clipboard fallback | 1.9.0 | PyPI, BSD-3-Clause |
| Autodesk Revit API | optional Revit integration | installed locally | Autodesk terms |

Versions in this table are the versions used to build and test release 0.1.0;
they are not a statement that newer releases do not exist. Lockfiles and
`requirements.txt` are the source of truth for reproducible installation.

No Autodesk binaries, browser profiles, cookies, Remote Commander identity or
third-party account credentials are stored in this repository.
