import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { readVersion, runtimeVersion } from '../src/desktop_agent/mcp/runtime-version.mjs';

test('MCP runtime advertises the checkout product version', () => {
  assert.equal(runtimeVersion, fs.readFileSync(new URL('../VERSION', import.meta.url), 'utf8').trim());
});

test('installed runtime uses copied VERSION; absent or malformed versions are explicit', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rdc-version-'));
  try {
    const desktop = path.join(root, 'runtime', 'desktop-agent');
    fs.mkdirSync(path.join(desktop, 'mcp'), { recursive: true });
    const base = pathToFileURL(path.join(desktop, 'mcp', 'runtime-version.mjs'));
    assert.equal(readVersion(base), 'unknown');
    fs.writeFileSync(path.join(desktop, 'VERSION'), '9.8.7\n');
    assert.equal(readVersion(base), '9.8.7');
    fs.writeFileSync(path.join(desktop, 'VERSION'), 'not-a-version');
    assert.equal(readVersion(base), 'unknown');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
