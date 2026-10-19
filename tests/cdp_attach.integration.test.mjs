import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {createBrowserSession} = require('../src/browser_bridge/browser_session.js');
const browserRequire = createRequire(new URL('../src/browser_bridge/package.json', import.meta.url));

test('real CDP attach/detach preserves the independently owned browser and does not persist its session',
  {skip: process.env.RDC_BROWSER_INTEGRATION !== '1', timeout: 60000}, async () => {
    const {chromium} = browserRequire('playwright');
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'rdc-cdp-fixture-'));
    const profile = path.join(root, 'external-profile');
    const stateFile = path.join(root, 'must-not-save.json');
    const owner = await chromium.launchPersistentContext(profile, {headless:true,
      args:['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0']});
    const session = createBrowserSession({chromium, profileDir:path.join(root, 'managed-profile'),
      stateFile, artifactDir:root});
    try {
      const page = owner.pages()[0] || await owner.newPage();
      await page.setContent('<label>Fixture name<input id="name"></label><label>Enabled<input type="checkbox" id="enabled"></label>');
      const lines = (await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).trim().split(/\r?\n/);
      const endpoint = `ws://127.0.0.1:${lines[0]}${lines[1]}`;
      await session.run({action:'attach', endpoint});
      const tab = (await session.run({action:'tabs'})).tabs[0];
      await session.run({action:'fill', tab_id:tab.tab_id, selector:'#name', value:'through-cdp'});
      await session.run({action:'setChecked', tab_id:tab.tab_id, selector:'#enabled', checked:true});
      assert.equal((await session.run({action:'inspect', tab_id:tab.tab_id, selector:'#name'})).element.value, 'through-cdp');
      await assert.rejects(fs.access(stateFile));
      await session.run({action:'detach'});
      assert.equal(page.isClosed(), false);
      assert.equal(await page.locator('#name').inputValue(), 'through-cdp');
      assert.equal(await page.locator('#enabled').isChecked(), true);
      assert.equal(owner.pages().length, 1);
      // Detach, reattach and close both use connection ownership, never the
      // borrowed default context's close method.
      await session.run({action:'attach', endpoint});
      await session.run({action:'close'});
      assert.equal(page.isClosed(), false);
      assert.equal(await page.title(), '');
      await assert.rejects(fs.access(stateFile));
    } finally {
      try { await session.shutdown(); } finally { await owner.close(); }
      await fs.rm(root, {recursive:true, force:true});
    }
  });
