import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { createBrowserSession, locatorFor, inspectElement } = require('../src/browser_bridge/browser_session.js');

const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

class FakeLocator {
  constructor(elements, index = null) { this.elements = elements; this.index = index; }
  matches() { return this.index == null ? this.elements : this.elements.slice(this.index, this.index + 1); }
  async count() { return this.matches().length; }
  nth(index) { return new FakeLocator(this.matches(), index); }
  async waitFor({ state }) {
    if (['hidden', 'detached'].includes(state) && !this.matches().length) return;
    if (this.matches().length !== 1) throw new Error('target missing or ambiguous');
  }
  only() { assert.equal(this.matches().length, 1); return this.matches()[0]; }
  async fill(value) { const target = this.only(); if (target.beforeFill) await target.beforeFill(); target.value = value; }
  async click() { this.only().clicks = (this.only().clicks || 0) + 1; }
  async setChecked(value) { this.only().checked = value; }
  async selectOption(value) { this.only().value = value; return Array.isArray(value) ? value : [value]; }
  async hover() { this.only().hovered = true; }
  async scrollIntoViewIfNeeded() { this.only().scrolled = true; }
  async innerText() { return this.only().text; }
  async evaluate(fn, arg) { return fn(this.only(), arg); }
  async elementHandle() {
    const element = this.only();
    return { contentFrame: async () => element.frame || null, dispose: async () => { element.disposed = true; } };
  }
}

class FakeFrame {
  constructor(name = '', parent = null) { this.frameName = name; this.parent = parent; this.elements = new Map(); this.href = 'about:blank'; }
  locator(selector) { return new FakeLocator(this.elements.get(selector) || []); }
  getByRole(role) { return this.locator(`role=${role}`); }
  name() { return this.frameName; }
  parentFrame() { return this.parent; }
  isDetached() { return !!this.detached; }
  url() { return this.href; }
  async title() { return this.frameName || 'Test'; }
  async goto(url) { this.href = url; }
}

class FakePage extends EventEmitter {
  constructor(context) {
    super(); this.context = context; this.main = new FakeFrame(); this.allFrames = [this.main];
    this.main.elements.set('#name', [{ value: '' }]);
  }
  locator(selector) { return this.main.locator(selector); }
  getByRole(role) { return this.main.getByRole(role); }
  mainFrame() { return this.main; }
  frames() { return this.allFrames.filter(frame => !frame.isDetached()); }
  url() { return this.main.url(); }
  async title() { return this.main.title(); }
  async goto(url) { await this.main.goto(url); }
  isClosed() { return !!this.closed; }
  async close() { this.closed = true; this.emit('close'); }
  async bringToFront() { this.front = true; }
}

function harness(options = {}) {
  const contexts = [];
  const stats = { launches: 0, saves: 0, contextCloses: 0, browserCloses: 0 };
  const chromium = {
    async launchPersistentContext(profile, config) {
      stats.launches++;
      assert.equal(profile, '/tmp/test-browser-profile');
      assert.equal(config.headless, !options.headed);
      if (options.beforeLaunch) await options.beforeLaunch(stats.launches);
      const context = new EventEmitter();
      context.allPages = [];
      context.pages = () => context.allPages.filter(p => !p.isClosed());
      context.browser = () => ({ close: async () => {
        stats.browserCloses++;
        if (options.beforeBrowserClose) await options.beforeBrowserClose();
        for (const p of context.allPages) await p.close();
        context.emit('close');
      } });
      context.newPage = async () => { const p = new FakePage(context); context.allPages.push(p); context.emit('page', p); return p; };
      context.storageState = async () => { stats.saves++; if (options.beforeSave) await options.beforeSave(stats.saves); };
      context.close = async () => {
        stats.contextCloses++;
        if (options.beforeContextClose) await options.beforeContextClose();
        for (const p of context.allPages) await p.close(); context.emit('close');
      };
      await context.newPage();
      contexts.push(context);
      return context;
    },
  };
  return { contexts, stats, session: createBrowserSession({ chromium, headed: !!options.headed, queueTimeoutMs: options.queueTimeoutMs,
    profileDir: '/tmp/test-browser-profile', stateFile: '/tmp/test-browser-state.json', artifactDir: '/tmp/test-artifacts' }) };
}

test('stable tab IDs survive index changes, explicit targeting does not switch the active tab', async () => {
  const h = harness();
  const first = (await h.session.run({ action: 'tabs' })).tabs[0];
  const second = await h.session.run({ action: 'newTab', url: 'https://second.test/' });
  const third = await h.session.run({ action: 'newTab', url: 'https://third.test/' });
  await h.session.run({ action: 'useTab', tab_id: second.tab_id });
  await h.session.run({ action: 'closeTab', tab_id: first.tab_id });
  await h.session.run({ action: 'fill', tab_id: third.tab_id, selector: '#name', value: 'third only' });
  assert.equal(h.contexts[0].allPages[2].main.elements.get('#name')[0].value, 'third only');
  assert.equal(h.contexts[0].allPages[1].main.elements.get('#name')[0].value, '');
  assert.equal((await h.session.run({ action: 'url' })).tab_id, second.tab_id);
  const tabs = (await h.session.run({ action: 'tabs' })).tabs;
  assert.equal(tabs.find(t => t.tab_id === third.tab_id).index, 1);
  await assert.rejects(h.session.run({ action: 'fill', tab_id: first.tab_id, selector: '#name', value: 'wrong' }),
    error => error.code === 'TAB_NOT_FOUND');
  assert.equal((await h.session.run({ action: 'useTab', index: 1 })).tab_id, third.tab_id);
  await h.session.shutdown();
});

test('popups are listed and wired once without stealing the active tab', async () => {
  const h = harness();
  const first = (await h.session.run({ action: 'tabs' })).tabs[0];
  const popup = await h.contexts[0].newPage();
  const popupInfo = (await h.session.run({ action: 'tabs' })).tabs.find(t => t.tab_id !== first.tab_id);
  assert.equal((await h.session.run({ action: 'url' })).tab_id, first.tab_id);
  assert.equal(popup.listenerCount('console'), 1);
  popup.emit('console', { type: () => 'log', text: () => 'popup message' });
  assert.equal((await h.session.run({ action: 'console', tab_id: first.tab_id })).items.length, 0);
  assert.equal((await h.session.run({ action: 'console', tab_id: popupInfo.tab_id, clear: true })).items.length, 1);
  assert.equal((await h.session.run({ action: 'console' })).items.length, 0);
  await h.session.shutdown();
});

test('restart invalidates old IDs; closing an inactive session does not launch a browser', async () => {
  const h = harness();
  await h.session.run({ action: 'close' });
  assert.equal(h.stats.launches, 0);
  const first = (await h.session.run({ action: 'tabs' })).tabs[0];
  const restarted = await h.session.run({ action: 'restart' });
  assert.notEqual(first.tab_id, restarted.tab_id);
  await assert.rejects(h.session.run({ action: 'url', tab_id: first.tab_id }), { code: 'TAB_NOT_FOUND' });
  await h.session.shutdown();
});

test('IDs from another bridge session cannot alias new tabs or frames after a watchdog replacement', async () => {
  const old = harness();
  const fresh = harness();
  const oldTab = (await old.session.run({ action: 'tabs' })).tabs[0];
  const oldFrame = (await old.session.run({ action: 'frames', tab_id: oldTab.tab_id })).frames[0];
  const freshTab = (await fresh.session.run({ action: 'tabs' })).tabs[0];
  const freshFrame = (await fresh.session.run({ action: 'frames', tab_id: freshTab.tab_id })).frames[0];
  assert.notEqual(oldTab.tab_id, freshTab.tab_id);
  assert.notEqual(oldFrame.frame_id, freshFrame.frame_id);
  await assert.rejects(fresh.session.run({ action: 'fill', tab_id: oldTab.tab_id, selector: '#name', value: 'wrong' }),
    { code: 'TAB_NOT_FOUND' });
  await assert.rejects(fresh.session.run({ action: 'fill', frame_id: oldFrame.frame_id, selector: '#name', value: 'wrong' }),
    { code: 'FRAME_NOT_FOUND' });
  assert.equal(fresh.contexts[0].pages()[0].main.elements.get('#name')[0].value, '');
  await old.session.shutdown();
  await fresh.session.shutdown();
});

test('concurrent launch/action/state save requests run in order and a failure does not poison the queue', async () => {
  const started = deferred();
  const finishSave = deferred();
  const h = harness({ beforeSave: async count => { if (count === 1) { started.resolve(); await finishSave.promise; } } });
  await Promise.all([h.session.run({ action: 'tabs' }), h.session.run({ action: 'tabs' })]);
  assert.equal(h.stats.launches, 1);
  const filling = h.session.run({ action: 'fill', selector: '#name', value: 'first action' });
  await started.promise;
  const opening = h.session.run({ action: 'newTab' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.contexts[0].pages().length, 1, 'newTab must wait for the prior storage snapshot');
  finishSave.resolve();
  await filling;
  const opened = await opening;
  await assert.rejects(h.session.run({ action: 'useTab', index: 999 }), { code: 'TAB_NOT_FOUND' });
  assert.equal((await h.session.run({ action: 'url' })).tab_id, opened.tab_id);
  await h.session.shutdown();
});

test('an expired queued write is skipped after its predecessor finishes', async () => {
  const started = deferred();
  const finish = deferred();
  const h = harness({ queueTimeoutMs: 20 });
  await h.session.run({ action: 'tabs' });
  const input = h.contexts[0].pages()[0].main.elements.get('#name')[0];
  input.beforeFill = async () => { started.resolve(); await finish.promise; };
  const running = h.session.run({ action: 'fill', selector: '#name', value: 'started action' });
  await started.promise;
  const expired = h.session.run({ action: 'fill', selector: '#name', value: 'must never execute' });
  await assert.rejects(expired, { code: 'QUEUE_TIMEOUT', action_started: false });
  assert.equal(input.value, '');
  finish.resolve();
  await running;
  await h.session.run({ action: 'url' });
  assert.equal(input.value, 'started action');
  await h.session.shutdown();
});

test('AbortSignal cancels only an unstarted queued write, never an action already running', async () => {
  const started = deferred();
  const finish = deferred();
  const h = harness();
  await h.session.run({ action: 'tabs' });
  const input = h.contexts[0].pages()[0].main.elements.get('#name')[0];
  input.beforeFill = async () => { started.resolve(); await finish.promise; };
  const runningAbort = new AbortController();
  const running = h.session.run({ action: 'fill', selector: '#name', value: 'started action' }, { signal: runningAbort.signal });
  await started.promise;
  const waitingAbort = new AbortController();
  const waiting = h.session.run({ action: 'fill', selector: '#name', value: 'cancelled action' }, { signal: waitingAbort.signal });
  const rejected = assert.rejects(waiting, { code: 'REQUEST_CANCELLED', action_started: false });
  waitingAbort.abort();
  runningAbort.abort();
  await rejected;
  finish.resolve();
  await running;
  await h.session.run({ action: 'url' });
  assert.equal(input.value, 'started action');
  await h.session.shutdown();
});

test('failed launch and failed state snapshot allow later requests to recover', async () => {
  const h = harness({ beforeLaunch: async count => { if (count === 1) throw new Error('launch failed'); },
    beforeSave: async count => { if (count === 1) throw new Error('state failed'); } });
  await assert.rejects(h.session.run({ action: 'tabs' }), /launch failed/);
  assert.equal((await h.session.run({ action: 'tabs' })).tabs.length, 1);
  const result = await h.session.run({ action: 'fill', selector: '#name', value: 'saved in DOM' });
  assert.equal(result.ok, true);
  assert.equal(result.action_completed, true);
  assert.equal(result.state_saved, false);
  assert.equal(result.state_warning.code, 'STATE_SAVE_FAILED');
  assert.match(result.state_warning.hint, /Do not repeat/);
  assert.equal(h.contexts[0].pages()[0].main.elements.get('#name')[0].value, 'saved in DOM');
  assert.equal((await h.session.run({ action: 'url' })).url, 'about:blank');
  assert.equal(h.stats.launches, 2);
  await h.session.shutdown();
});

test('close still attempts context and browser cleanup when state saving fails', async () => {
  const h = harness({ beforeSave: async () => { throw new Error('state disk unavailable'); } });
  await h.session.run({ action: 'tabs' });
  const result = await h.session.run({ action: 'close' });
  assert.equal(result.ok, true);
  assert.equal(result.action_completed, true);
  assert.equal(result.state_saved, false);
  assert.equal(h.stats.contextCloses, 1);
  assert.equal(h.stats.browserCloses, 1);
  assert.equal(h.contexts[0].allPages[0].isClosed(), true);
  assert.equal((await h.session.run({ action: 'status' })).running, false);
});

test('failed cleanup retains references for retry; browser fallback closes a failed context', async () => {
  let failCleanup = true;
  const h = harness({ beforeContextClose: async () => { if (failCleanup) throw new Error('context close failed'); },
    beforeBrowserClose: async () => { if (failCleanup) throw new Error('browser close failed'); } });
  await h.session.run({ action: 'tabs' });
  await assert.rejects(h.session.run({ action: 'close' }), { code: 'BROWSER_CLOSE_FAILED' });
  assert.equal((await h.session.run({ action: 'status' })).running, true);
  failCleanup = false;
  await h.session.run({ action: 'close' });
  assert.equal((await h.session.run({ action: 'status' })).running, false);
  const fallback = harness({ beforeContextClose: async () => { throw new Error('context close failed'); } });
  await fallback.session.run({ action: 'tabs' });
  const closed = await fallback.session.run({ action: 'close' });
  assert.match(closed.cleanup_warning, /context close failed/);
  assert.equal(fallback.stats.browserCloses, 1);
  assert.equal((await fallback.session.run({ action: 'status' })).running, false);
});

test('strict locators reject ambiguity; nth is an explicit, validated choice', async () => {
  const frame = new FakeFrame();
  const buttons = [{}, {}];
  frame.elements.set('button', buttons);
  await assert.rejects(locatorFor(frame, { selector: 'button' }), error =>
    error.code === 'AMBIGUOUS_LOCATOR' && error.count === 2 && /nth/.test(error.hint));
  await (await locatorFor(frame, { selector: 'button', nth: 1 })).click();
  assert.equal(buttons[0].clicks, undefined);
  assert.equal(buttons[1].clicks, 1);
  for (const nth of [-1, 0.5, '1']) await assert.rejects(locatorFor(frame, { selector: 'button', nth }), /nth must/);
  await assert.rejects(locatorFor(frame, { selector: 'button', nth: 2 }), { code: 'LOCATOR_NOT_FOUND', count: 2 });
  await assert.rejects(locatorFor(frame, { selector: 'missing' }), { code: 'LOCATOR_NOT_FOUND', count: 0 });
  await (await locatorFor(frame, { selector: 'missing' }, { allowMissing: true })).waitFor({ state: 'hidden' });
});

test('nested frame IDs/selectors target the intended frame and reject stale or wrong-tab IDs', async () => {
  const h = harness();
  const tab = (await h.session.run({ action: 'tabs' })).tabs[0];
  const p = h.contexts[0].pages()[0];
  const outer = new FakeFrame('outer', p.main);
  const inner = new FakeFrame('inner', outer);
  const input = { value: '' };
  p.allFrames.push(outer, inner);
  p.main.elements.set('#outer', [{ frame: outer }]);
  outer.elements.set('#inner', [{ frame: inner }]);
  inner.elements.set('#name', [input]);
  const result = await h.session.run({ action: 'frames', tab_id: tab.tab_id });
  const innerId = result.frames.find(f => f.name === 'inner').frame_id;
  assert.equal(result.frames.find(f => f.name === 'outer').parent_frame_id, result.frames.find(f => f.is_main).frame_id);
  await h.session.run({ action: 'fill', tab_id: tab.tab_id, frame_id: innerId, selector: '#name', value: 'by id' });
  assert.equal(input.value, 'by id');
  await h.session.run({ action: 'fill', frame_selector: ['#outer', '#inner'], selector: '#name', value: 'by selector' });
  assert.equal(input.value, 'by selector');
  assert.equal(p.main.elements.get('#name')[0].value, '');
  assert.equal(outer.elements.get('#inner')[0].disposed, true);
  assert.equal((await h.session.run({ action: 'frames', max_frames: 1 })).truncated, true);
  const other = await h.session.run({ action: 'newTab' });
  await assert.rejects(h.session.run({ action: 'fill', tab_id: other.tab_id, frame_id: innerId, selector: '#name', value: 'wrong' }),
    { code: 'FRAME_NOT_FOUND' });
  inner.detached = true;
  await assert.rejects(h.session.run({ action: 'fill', tab_id: tab.tab_id, frame_id: innerId, selector: '#name', value: 'stale' }),
    { code: 'FRAME_NOT_FOUND' });
  await assert.rejects(h.session.run({ action: 'reload', frame_id: innerId }), /does not support frame targeting/);
  await h.session.shutdown();
});

test('semantic form actions validate intent and target only one control', async () => {
  const h = harness();
  await h.session.run({ action: 'tabs' });
  const elements = h.contexts[0].pages()[0].main.elements;
  const input = elements.get('#name')[0];
  await h.session.run({ action: 'setChecked', selector: '#name', checked: true });
  await h.session.run({ action: 'setChecked', selector: '#name', checked: true });
  assert.equal(input.checked, true, 'setChecked is idempotent');
  await assert.rejects(h.session.run({ action: 'setChecked', selector: '#name', checked: 'false' }), /requires checked/);
  assert.equal(input.checked, true);
  const selected = await h.session.run({ action: 'selectOption', selector: '#name', value: ['ru', 'en'] });
  assert.deepEqual(selected.values, ['ru', 'en']);
  await assert.rejects(h.session.run({ action: 'selectOption', selector: '#name', value: 1 }), /requires value/);
  await h.session.run({ action: 'hover', selector: '#name' });
  await h.session.run({ action: 'scrollIntoView', selector: '#name' });
  assert.equal(input.hovered, true);
  assert.equal(input.scrolled, true);
  await h.session.shutdown();
});

test('inspect bounds option text and redacts password values', async () => {
  const element = { tagName: 'INPUT', type: 'password', value: 'do-not-return-this', textContent: '',
    ownerDocument: { getElementById: () => null }, labels: [],
    getAttribute: name => ({ 'aria-label': 'Password', type: 'password' })[name] || null,
    matches: () => false };
  const result = await inspectElement(new FakeLocator([element]), {});
  assert.equal(result.name, 'Password');
  assert.equal(result.value, '[REDACTED]');
  assert.equal(result.value_redacted, true);
  assert.equal(JSON.stringify(result).includes(element.value), false);
  const select = { ...element, tagName: 'SELECT', type: 'select-one', value: 'A', textContent: 'Long select text',
    getAttribute: () => null, options: ['A', 'B', 'C'].map(value => ({ value, label: value.repeat(20),
      selected: value === 'A', disabled: false, closest: () => null })) };
  const state = await inspectElement(new FakeLocator([select]), { max_length: 10, max_options: 2 });
  assert.equal(state.options.length, 2);
  assert.equal(state.options[0].label.length, 10);
  assert.equal(state.options_truncated, true);
  assert.equal(state.option_count, 3);
  await assert.rejects(inspectElement(new FakeLocator([select]), { max_options: 10000 }), /max_options must/);
});
