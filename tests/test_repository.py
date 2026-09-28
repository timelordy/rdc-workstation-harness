import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".ps1", ".md", ".json", ".yml", ".yaml"}


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def repository_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "node_modules" in path.parts:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path


class RepositoryContractTests(unittest.TestCase):
    def test_readme_local_markdown_links_exist(self):
        links = re.findall(r"\]\((?!https?://)([^)#]+\.md)(?:#[^)]+)?\)", read("README.md"))
        self.assertTrue(links)
        missing = [link for link in links if not (ROOT / link).exists()]
        self.assertEqual([], missing)
    def test_examples_are_valid_json_actions(self):
        examples = sorted((ROOT / "examples").glob("*.json"))
        self.assertTrue(examples, "examples directory must not be empty")
        for path in examples:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict, path.name)
            self.assertIn("action", payload, path.name)

    def test_no_runtime_secrets_or_generated_binaries(self):
        forbidden_names = {
            "device.json", "desktop.token", "browser.token",
            "storage-state.json",
        }
        forbidden_suffixes = {".dll", ".pdb", ".pyc", ".zip"}
        offenders = []
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or ".git" in path.parts
                or "__pycache__" in path.parts
            ):
                continue
            if path.name.lower() in forbidden_names or path.suffix.lower() in forbidden_suffixes:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_no_machine_specific_absolute_paths(self):
        offenders = []
        for path in repository_text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"C:\\Users\\[^%]", text, re.IGNORECASE):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)
    def test_loopback_only_bindings_are_code_invariants(self):
        agent = read("src/desktop_agent/agent.py")
        browser = read("src/browser_bridge/bridge.js")
        self.assertIn('HOST = "127.0.0.1"', agent)
        self.assertNotIn('HOST = "0.0.0.0"', agent)
        self.assertIn("server.listen(PORT, '127.0.0.1'", browser)
        self.assertNotIn("server.listen(PORT, '0.0.0.0'", browser)

    def test_physical_input_is_disabled_by_default(self):
        agent = read("src/desktop_agent/agent.py")
        self.assertIn("PHYSICAL_INPUT_DEFAULT = False", agent)
        self.assertIn("require_physical(a)", agent)

    def test_installer_config_here_string_is_not_corrupted(self):
        installer = read("scripts/install.ps1")
        self.assertNotIn('$Config += @"', installer)
        self.assertIn("`$env:NODE_EXE", installer)
        self.assertIn("Write-PowerShellFile $ConfigPath $Config", installer)
        self.assertIn('$Config += "`r`n`$env:PW_EXECUTABLE', installer)

    def test_pc_agent_is_installed_on_path(self):
        installer = read("scripts/install.ps1")
        uninstaller = read("scripts/uninstall.ps1")
        self.assertTrue((ROOT / "tools" / "pc-agent.cmd").exists())
        self.assertIn('SetEnvironmentVariable("Path"', installer)
        self.assertIn("$ToolsDir", installer)
        self.assertIn('SetEnvironmentVariable("Path"', uninstaller)
        wrapper = (ROOT / "tools" / "pc-agent.cmd").read_bytes()
        self.assertIn(b"\r\n", wrapper)
        self.assertIn(b"pc_agent.py", wrapper)

    def test_remote_commander_runs_with_share_hook(self):
        installer = read("scripts/install.ps1")
        self.assertIn("tools\\rdc_share_hook.mjs", installer)
        self.assertIn("--import `$hook", installer)
        self.assertTrue((ROOT / "tools" / "rdc_share_hook.mjs").exists())

    def test_version_and_changelog_match(self):
        version = read("VERSION").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertIn(f"## [{version}]", read("CHANGELOG.md"))

    def test_update_navigator_contract(self):
        installer = read("scripts/install.ps1")
        checker = read("scripts/check-update.ps1")
        updater = read("scripts/update.ps1")
        self.assertIn('ValidateSet("stable", "main")', installer)
        self.assertIn('Register-UpdateTask "$TaskPrefix - Update Check"', installer)
        self.assertIn('install-state.json', installer)
        self.assertIn('/releases/latest', checker)
        self.assertIn('/commits/main', checker)
        self.assertIn('update-available.json', checker)
        self.assertIn('pre-update-', updater)
        self.assertIn('Expand-Archive', updater)

    def test_gitignore_covers_sensitive_runtime_state(self):
        ignore = read(".gitignore")
        for item in ("*.token", ".desktop-commander-device/", "browser-profile/", "storage-state.json",
                     "local/*", "rclone.conf", "share-*.ps1"):
            self.assertIn(item, ignore)

    def test_local_dir_holds_only_readme(self):
        tracked = [p.name for p in (ROOT / "local").iterdir() if p.is_file()]
        # Only README may exist in a fresh clone; other files are private and ignored.
        self.assertIn("README.md", tracked)

    def test_no_uploader_implementation_in_repo(self):
        # The repo ships the share mechanism only: no uploader script and no
        # cloud CLI calls. Which cloud is used is local configuration.
        self.assertEqual([], [p.name for p in (ROOT / "tools").glob("share-*.ps1")])
        offenders = []
        pattern = re.compile(r"\brclone(\.exe)?\s+(copy|copyto|link|config)\b", re.I)
        for path in repository_text_files():
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_secret_scanning_is_wired(self):
        hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("gitleaks git --staged", hook)
        self.assertIn("local/denylist.txt", hook)
        self.assertIn("gitleaks.exe git --no-banner --redact --log-opts=\"--all\"", read(".github/workflows/ci.yml"))
        self.assertTrue((ROOT / ".gitleaks.toml").exists())


if __name__ == "__main__":
    unittest.main()
