# Security Policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Intended boundary

This project is designed for one trusted Windows workstation and one trusted
user session. Its own services must remain bound to `127.0.0.1`. Local
credentials and browser state stay outside the repository.

Do not expose ports 17321–17322 directly to a LAN, VPN or the public internet.
Do not copy a workstation identity or browser profile to another computer.

## Reporting

Use GitHub private vulnerability reporting for security-sensitive findings.
Describe the affected component, version, reproduction steps and impact.
Remove personal, company and workstation data from any evidence.

## Recovery

When local credentials or browser state may have been disclosed:

1. Stop the harness.
2. Re-run `scripts\install.ps1 -RotateTokens`.
3. Sign out of affected browser sessions when appropriate.
4. Re-pair Remote Commander when its device identity is affected.
5. Review local logs and connected-client history.

Never publish `%USERPROFILE%\.chatgpt-desktop-agent`,
`%USERPROFILE%\.desktop-commander-device` or a browser profile.
