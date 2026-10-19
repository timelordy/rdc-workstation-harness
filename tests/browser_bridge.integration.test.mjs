// Opt in after `npm ci` and `npx playwright install chromium` in src/browser_bridge:
// RDC_BROWSER_INTEGRATION=1 node --test tests/browser_bridge.integration.test.mjs
// Every run uses a fresh profile and serves only the fixture below on loopback.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { createBrowserSession } = require('../src/browser_bridge/browser_session.js');

const fixtures = {
  '/': `<!doctype html><html><head><title>Browser control fixture</title></head><body>
    <label for="name">Full name</label><input id="name">
    <label for="password">Password</label><input id="password" type="password" value="fixture-secret-do-not-return">
    <label for="country">Country</label><select id="country"><option value="">Choose</option>
      <option value="ru">Russia</option><option value="de">Germany</option></select>
    <label><input id="terms" type="checkbox">Accept terms</label>
    <button class="duplicate" onclick="document.querySelector('#result').textContent='first'">Duplicate</button>
    <button class="duplicate" onclick="document.querySelector('#result').textContent='second'">Duplicate</button>
    <p id="result">unchanged</p>
    <iframe id="outer" name="outer" src="/outer"></iframe>
    <button id="popup" onclick="window.open('/popup')">Open popup</button>
    <div style="height:1800px"></div><button id="bottom" onmouseenter="this.textContent='Hovered'">Bottom</button>
    </body></html>`,
  '/outer': '<!doctype html><html><head><title>Outer frame</title></head><body><iframe id="inner" name="inner" src="/inner"></iframe></body></html>',
  '/inner': '<!doctype html><html><head><title>Inner frame</title></head><body><label for="name">Full name</label><input id="name"></body></html>',
  '/popup': '<!doctype html><html><head><title>Popup</title></head><body>Popup fixture</body></html>',
};

test('real Chromium: stable tabs, strict controls, iframe targeting and bounded inspection', {
  skip: process.env.RDC_BROWSER_INTEGRATION !== '1', timeout: 60000,
}, async t => {
  const { chromium } = require('../src/browser_bridge/node_modules/playwright');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'rdc-browser-integration-'));
  const server = http.createServer((request, response) => {
    response.writeHead(fixtures[request.url] ? 200 : 404, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(fixtures[request.url] || 'Not found');
  });
  let realContext;
  const session = createBrowserSession({
    chromium: { async launchPersistentContext(...args) {
      realContext = await chromium.launchPersistentContext(...args);
      return realContext;
    } },
    profileDir: path.join(temp, 'profile'), stateFile: path.join(temp, 'storage-state.json'),
    artifactDir: temp, executablePath: process.env.PW_EXECUTABLE,
  });
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const origin = `http://127.0.0.1:${server.address().port}`;
    const initial = await session.run({ action: 'open', url: origin, waitUntil: 'load' });
    const tab_id = initial.tab_id;

    await t.test('ambiguity never clicks silently; explicit nth selects the requested match', async () => {
      await assert.rejects(session.run({ action: 'click', tab_id, role: 'button', name: 'Duplicate', exact: true }),
        { code: 'AMBIGUOUS_LOCATOR', count: 2 });
      assert.equal((await session.run({ action: 'text', tab_id, selector: '#result' })).text, 'unchanged');
      await session.run({ action: 'click', tab_id, role: 'button', name: 'Duplicate', exact: true, nth: 1 });
      assert.equal((await session.run({ action: 'text', tab_id, selector: '#result' })).text, 'second');
      await assert.rejects(session.run({ action: 'fill', tab_id, selector: '#absent', value: 'x', timeout: 50 }),
        { code: 'LOCATOR_NOT_FOUND', count: 0 });
      await session.run({ action: 'wait', tab_id, selector: '#absent', state: 'hidden', timeout: 50 });
    });

    await t.test('semantic form operations expose bounded state and redact password values', async () => {
      await session.run({ action: 'fill', tab_id, label: 'Full name', exact: true, value: 'Test User' });
      assert.equal((await session.run({ action: 'inspect', tab_id, label: 'Full name', exact: true })).element.value, 'Test User');
      await session.run({ action: 'selectOption', tab_id, label: 'Country', value: 'ru' });
      const select = (await session.run({ action: 'inspect', tab_id, label: 'Country', max_options: 2 })).element;
      assert.equal(select.value, 'ru');
      assert.equal(select.options[1].selected, true);
      assert.equal(select.options_truncated, true);
      await session.run({ action: 'setChecked', tab_id, label: 'Accept terms', checked: true });
      await session.run({ action: 'setChecked', tab_id, label: 'Accept terms', checked: true });
      assert.equal((await session.run({ action: 'inspect', tab_id, label: 'Accept terms' })).element.checked, true);
      const password = (await session.run({ action: 'inspect', tab_id, label: 'Password' })).element;
      assert.equal(password.value, '[REDACTED]');
      assert.equal(password.value_redacted, true);
      assert.equal(JSON.stringify(password).includes('fixture-secret-do-not-return'), false);
      await session.run({ action: 'scrollIntoView', tab_id, selector: '#bottom' });
      await session.run({ action: 'hover', tab_id, selector: '#bottom' });
      assert.equal((await session.run({ action: 'text', tab_id, selector: '#bottom' })).text, 'Hovered');
    });

    await t.test('nested frames can be targeted by selector chain or live frame ID', async () => {
      await session.run({ action: 'fill', tab_id, frame_selector: ['#outer', '#inner'], label: 'Full name', value: 'Nested' });
      const frames = (await session.run({ action: 'frames', tab_id })).frames;
      const inner = frames.find(frame => frame.name === 'inner');
      assert.ok(inner && inner.parent_frame_id);
      const nested = await session.run({ action: 'inspect', tab_id, frame_id: inner.frame_id, label: 'Full name' });
      assert.equal(nested.element.value, 'Nested');
      assert.equal((await session.run({ action: 'inspect', tab_id, label: 'Full name' })).element.value, 'Test User');
    });

    await t.test('popups do not switch the active page; surviving tab IDs stay valid after indexes shift', async () => {
      const popupReady = realContext.waitForEvent('page');
      await session.run({ action: 'click', tab_id, selector: '#popup' });
      const popup = await popupReady;
      await popup.waitForLoadState('domcontentloaded');
      assert.equal((await session.run({ action: 'url' })).tab_id, tab_id);
      const opened = await session.run({ action: 'newTab', url: origin });
      await session.run({ action: 'closeTab', tab_id });
      await session.run({ action: 'fill', tab_id: opened.tab_id, label: 'Full name', exact: true, value: 'Stable target' });
      assert.equal((await session.run({ action: 'inspect', tab_id: opened.tab_id, label: 'Full name', exact: true })).element.value,
        'Stable target');
      await assert.rejects(session.run({ action: 'inspect', tab_id, label: 'Full name' }), { code: 'TAB_NOT_FOUND' });
    });
  } finally {
    try { await session.shutdown(); } finally {
      if (server.listening) await new Promise(resolve => server.close(resolve));
      fs.rmSync(temp, { recursive: true, force: true });
    }
  }
});
