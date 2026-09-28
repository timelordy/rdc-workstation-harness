// node --test tests/share_hook.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

process.env.RDC_HARNESS_SHARE_HOOK_LOG = path.join(os.tmpdir(), `share-hook-test-${process.pid}.ndjson`);
const hook = await import('../tools/rdc_share_hook.mjs');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'share-hook-'));
const png = path.join(tmp, 'shot.png');
const txt = path.join(tmp, 'notes.txt');
fs.writeFileSync(png, 'png');
fs.writeFileSync(txt, 'hello');

const imageResult = () => ({
  content: [
    { type: 'text', text: `Image file: ${png}` },
    { type: 'image', data: 'AAAA', mimeType: 'image/png' },
  ],
});
const on = () => true;
const off = () => false;
const fakeShare = async () => 'https://example.test/s/1';
const chatgpt = { remote: true, clientInfo: { name: 'openai-mcp', version: '1' } };
const claude = { remote: true, clientInfo: { name: 'claude-ai', version: '1' } };

test('ChatGPT image read: image block dropped, link added', async () => {
  const out = await hook.augmentResult('read_file', { path: png }, chatgpt, imageResult(), fakeShare, on);
  assert.equal(out.content.some((c) => c.type === 'image'), false);
  assert.match(out.content.at(-1).text, /https:\/\/example\.test\/s\/1/);
});

test('Claude keeps the image and also gets the link', async () => {
  const out = await hook.augmentResult('read_file', { path: png }, claude, imageResult(), fakeShare, on);
  assert.equal(out.content.some((c) => c.type === 'image'), true);
  assert.match(out.content.at(-1).text, /example\.test/);
});

test('not configured: result untouched, nothing shared', async () => {
  let called = false;
  const original = imageResult();
  const out = await hook.augmentResult('read_file', { path: png }, chatgpt, original,
    async () => { called = true; return 'x'; }, off);
  assert.equal(out, original);
  assert.equal(called, false);
});

test('text files and other tools are ignored', async () => {
  const r = { content: [{ type: 'text', text: 'hello' }] };
  assert.equal(await hook.augmentResult('read_file', { path: txt }, chatgpt, r, fakeShare, on), r);
  assert.equal(await hook.augmentResult('list_directory', { path: tmp }, chatgpt, r, fakeShare, on), r);
});

test('share failure fails open', async () => {
  const fresh = path.join(tmp, 'offline.png');  // not in the share cache yet
  fs.writeFileSync(fresh, 'png');
  const original = imageResult();
  const out = await hook.augmentResult('read_file', { path: fresh }, chatgpt, original,
    async () => { throw new Error('offline'); }, on);
  assert.equal(out, original);
});

test('same file is shared once', async () => {
  const again = path.join(tmp, 'again.jpg');
  fs.writeFileSync(again, 'jpg');
  let calls = 0;
  const share = async () => { calls += 1; return 'https://example.test/s/2'; };
  await hook.augmentResult('read_file', { path: again }, chatgpt, imageResult(), share, on);
  await hook.augmentResult('read_file', { path: again }, chatgpt, imageResult(), share, on);
  assert.equal(calls, 1);
});
