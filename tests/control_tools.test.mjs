import assert from 'node:assert/strict';
import test from 'node:test';
import { browserRequestSchema, registerControlTools } from '../src/desktop_agent/mcp/control-tools.mjs';

test('structured browser input catches form mistakes before dispatch', () => {
  for (const request of [
    { action: 'fill', label: 'Name' },
    { action: 'setChecked', label: 'Enabled', checked: 'true' },
    { action: 'click', role: 'button', text: 'Save' },
    { action: 'click', label: 'Name', nth: -1 },
    { action: 'click', label: 'Name', frame_id: 'frame-a', frame_selector: '#frame' },
    { action: 'goto' },
    { action: 'misspelled' },
  ]) assert.equal(browserRequestSchema.safeParse(request).success, false, JSON.stringify(request));
});

test('browser schema preserves explicit target and documented advanced options', () => {
  const request = {
    action: 'selectOption', tab_id: 'tab-a', frame_selector: ['#outer', '#inner'],
    label: 'Discipline', exact: true, nth: 1, value: ['architecture', 'structure'], timeout: 20000,
  };
  assert.deepEqual(browserRequestSchema.parse(request), request);
  const advanced = { action: 'screenshot', tab_id: 'tab-a', fullPage: false, path: 'shot.png' };
  assert.deepEqual(browserRequestSchema.parse(advanced), advanced);
});

test('browser tool forwards the existing commit gate without promoting payload flags', async () => {
  const registered = new Map();
  const calls = [];
  registerControlTools({ registerTool: (name, config, call) => registered.set(name, { config, call }) },
    async request => { calls.push(request); return { ok: true }; });
  const action = { action: 'click', role: 'button', name: 'Submit', tab_id: 'tab-a' };
  await registered.get('desktop_browser').call({ request: { ...action, commit: true } });
  assert.equal(calls[0].commit, false);
  await registered.get('desktop_browser').call({ request: action, commit: true });
  assert.deepEqual(calls[1], { action: 'browser', payload: action, commit: true });
  assert.equal(registered.get('desktop_inspect').config.annotations.readOnlyHint, true);
  await registered.get('desktop_inspect').call({ window: { pid: 1234 }, max_text: 0 });
  assert.deepEqual(calls[2], { action: 'uia_inspect', window: { pid: 1234 }, max_text: 0 });
});
