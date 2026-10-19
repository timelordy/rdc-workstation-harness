import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {createRequire} from 'node:module';
import {appRequestSchema, browserRequestSchema, registerControlTools} from '../src/desktop_agent/mcp/control-tools.mjs';
const require = createRequire(import.meta.url);
const {createBrowserSession} = require('../src/browser_bridge/browser_session.js');

const endpoint = 'ws://127.0.0.1:9222/devtools/browser/fixture-id';
function fixture() {
  let connected = 0, launched = 0, closed = 0, saved = 0, contextClosed = 0, created = 0;
  const page = Object.assign(new EventEmitter(), {isClosed: () => false, url: () => 'about:blank', title: async () => 'fixture'});
  const context = Object.assign(new EventEmitter(), {pages: () => [page],
    storageState: async () => {saved++;}, close: async () => {contextClosed++;},
    newPage: async () => {created++; return page;}});
  const browser = Object.assign(new EventEmitter(), {contexts: () => [context], close: async () => {closed++; browser.emit('disconnected');}});
  const chromium = {connectOverCDP: async () => {connected++; return browser;},
    launchPersistentContext: async () => {launched++; throw Error('unexpected managed launch');}};
  const session = createBrowserSession({chromium, profileDir:'fixture-profile', stateFile:'fixture-state', artifactDir:'fixture-artifacts'});
  return {session, context, browser, stats: () => ({connected, launched, closed, saved, contextClosed, created})};
}

test('CDP uses a borrowed session; detach neither closes the context nor saves cookies/storage', async () => {
  const f = fixture();
  const attached = await f.session.run({action:'attach', endpoint});
  assert.equal(attached.ownership, 'attached');
  assert.equal((await f.session.run({action:'tabs'})).tabs.length, 1);
  assert.equal((await f.session.run({action:'status'})).state_persistence, false);
  await assert.rejects(f.session.run({action:'restart'}), /not allowed/);
  await assert.rejects(f.session.run({action:'attach', endpoint}), /already active/);
  const result = await f.session.run({action:'detach'});
  assert.equal(result.external_application_closed, false);
  assert.deepEqual(f.stats(), {connected:1, launched:0, closed:1, saved:0, contextClosed:0, created:0});
  await f.session.shutdown();
});

test('CDP connection loss never silently launches a different browser', async () => {
  const f = fixture();
  await f.session.run({action:'attach', endpoint});
  f.browser.emit('disconnected');
  await assert.rejects(f.session.run({action:'tabs'}), error => error.code === 'EXTERNAL_DISCONNECTED');
  assert.equal(f.stats().launched, 0);
  await f.session.run({action:'detach'});
});

test('CDP rejects non-loopback hosts, discovery redirects, credentials and ambiguous endpoints before connecting', async () => {
  const f = fixture();
  for (const value of ['http://127.0.0.1:9222', 'ws://example.test:9222/devtools/browser/id',
    'ws://localhost:9222/devtools/browser/id', 'ws://0.0.0.0:9222/devtools/browser/id',
    'ws://u:p@127.0.0.1:9222/devtools/browser/id', endpoint+'?token=abc', endpoint+'#part',
    'ws://127.0.0.1:9222/devtools/page/id', 'ws://127.0.0.1:0/devtools/browser/id', 'not-a-url']) {
    await assert.rejects(f.session.run({action:'attach', endpoint:value}), /endpoint|Use ws/);
  }
  assert.equal(f.stats().connected, 0);
  assert.equal(f.stats().launched, 0);
});

test('universal application request schema catches invalid operations and ambiguous boolean values', () => {
  assert.equal(appRequestSchema.safeParse({action:'observe', hwnd:1234}).success, true);
  for (const value of [{action:'observe'}, {action:'act', ref:'x', operation:'click_input'},
    {action:'act', ref:'x', operation:'set_checked', request_id:'one', checked:'false'},
    {action:'wait', ref:'x', expected:{}}, {action:'open', executable:'demo.exe', args:'--help'},
    {action:'observe', hwnd:1234, backend:'specific-application'}, {action:'catalog', commit:true}]) {
    assert.equal(appRequestSchema.safeParse(value).success, false, JSON.stringify(value));
  }
  assert.equal(browserRequestSchema.safeParse({action:'attach'}).success, false);
  assert.equal(browserRequestSchema.safeParse({action:'attach', endpoint}).success, true);
});

test('universal MCP tool forwards only its explicit top-level commit decision', async () => {
  const tools = new Map(), requests = [];
  registerControlTools({registerTool: (name, definition, handler) => tools.set(name, {definition, handler})}, async a => {requests.push(a); return a;});
  const tool = tools.get('desktop_app');
  assert.ok(tool);
  const request = {action:'act', ref:'control-ref', operation:'invoke', request_id:'one'};
  await tool.handler({request, commit:false});
  assert.equal(requests[0].action, 'app_act');
  assert.equal(requests[0].commit, false);
  await tool.handler({request, commit:true});
  assert.equal(requests[1].commit, true);
});
