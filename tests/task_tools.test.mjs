import assert from 'node:assert/strict';
import test from 'node:test';
import {
  prepareTaskSchema, registerTaskTools, skillsRequestSchema, taskRequestSchema,
} from '../src/desktop_agent/mcp/task-tools.mjs';

test('skill discovery preserves explicit scope and package identity', () => {
  for (const input of [
    { action: 'list' },
    { action: 'list', workspace: 'C:\\Work\\Project' },
    { action: 'resolve', goal: 'Validate a CSV', requested_skills: [], limit: 2 },
    { action: 'read', skill_id: 'builtin:validate-table', resource: 'references/checks.md', expected_hash: 'a'.repeat(64) },
  ]) assert.deepEqual(skillsRequestSchema.parse(input), input);
});

test('skill schema rejects unknown actions and cannot add trust or run scripts', () => {
  for (const input of [
    { action: 'install', skill_id: 'external' },
    { action: 'list', trust_workspace: true },
    { action: 'read', skill_id: 'builtin:validate-table', execute: true },
    { action: 'read', skill_id: 'builtin:validate-table', expected_hash: 'old' },
    { action: 'resolve', goal: ' ' },
    { action: 'resolve', goal: 'Inspect a file', requested_skills: ['a', 'b', 'c', 'd'] },
  ]) assert.equal(skillsRequestSchema.safeParse(input).success, false, JSON.stringify(input));
});

test('preparation accepts declared expected outputs without untyped policy fields', () => {
  const input = {
    goal: 'Check the exported data', workspace: 'C:\\Work\\Project',
    requested_skills: ['builtin:validate-table'],
    check_inputs: { 'builtin:validate-table/schema': { required_columns: ['id', 'name'], min_rows: 2, delimiter: ';' } },
  };
  assert.deepEqual(prepareTaskSchema.parse(input), input);
  for (const invalid of [
    { ...input, action: 'exec' },
    { ...input, commit: true },
    { goal: 'test', check_inputs: { check: { unrecognized: true } } },
    { goal: 'test', check_inputs: { check: { min_rows: -1 } } },
    { goal: 'test', check_inputs: { check: { min_rows: 100001 } } },
    { goal: 'test', check_inputs: { check: { required_columns: 'id' } } },
    { goal: 'test', check_inputs: { check: { required_columns: ['id', 'id'] } } },
    { goal: 'test', check_inputs: { check: { delimiter: '\n' } } },
    { goal: 'test', check_inputs: { check: { min_bytes: true } } },
    { goal: 'test', check_inputs: { check: { min_bytes: 16 * 1024 * 1024 + 1 } } },
  ]) assert.equal(prepareTaskSchema.safeParse(invalid).success, false, JSON.stringify(invalid));
});

test('MCP bounds match Python verifier and resource limits', () => {
  const required_columns = Array.from({ length: 256 }, (_, index) => `column${index}`);
  const input = { goal: 'Wide CSV', check_inputs: { 'skill/schema': { required_columns } } };
  assert.deepEqual(prepareTaskSchema.parse(input), input);
  assert.equal(prepareTaskSchema.safeParse({ goal: 'Use default file limit', check_inputs: { 'skill/file': {} } }).success, true);
  assert.equal(prepareTaskSchema.safeParse({ ...input, check_inputs: { 'skill/schema': { required_columns: [...required_columns, 'overflow'] } } }).success, false);
  assert.equal(skillsRequestSchema.safeParse({ action: 'read', skill_id: 'x'.repeat(512) }).success, true);
  assert.equal(prepareTaskSchema.safeParse({ goal: 'test', requested_skills: ['x'.repeat(257)] }).success, false);
  assert.equal(skillsRequestSchema.safeParse({ action: 'read', skill_id: 'x', resource: 'x'.repeat(513) }).success, false);
  assert.equal(taskRequestSchema.safeParse({ action: 'check', task_id: 'task-1', check_id: 'x'.repeat(401), path: 'x' }).success, false);
});

test('task check distinguishes an artifact for verification from self-reported evidence', () => {
  for (const request of [
    { action: 'check', task_id: 'task-1', check_id: 'skill/file', path: 'output.csv' },
    { action: 'check', task_id: 'task-1', check_id: 'skill/target', status: 'passed', evidence: 'Window title and document path match.' },
    { action: 'check', task_id: 'task-1', check_id: 'skill/optional', status: 'not_applicable', evidence: 'No worksheet present.', reason: 'Input is CSV.' },
  ]) assert.deepEqual(taskRequestSchema.parse(request), request);
});

test('task schemas reject fake success and modifications to pinned expectations', () => {
  const base = { action: 'check', task_id: 'task-1', check_id: 'skill/check' };
  for (const request of [
    base,
    { ...base, status: 'passed' },
    { ...base, status: 'verified', evidence: 'trust me' },
    { ...base, status: 'passed', evidence: ' ' },
    { ...base, status: 'not_applicable', evidence: 'not needed' },
    { ...base, path: 'output.csv', status: 'passed' },
    { ...base, path: 'output.csv', evidence: 'trust me' },
    { ...base, path: 'output.csv', verified: true },
    { ...base, path: 'output.csv', min_rows: 0 },
    { ...base, path: 'output.csv', expectations: { min_rows: 0 } },
    { action: 'finish', task_id: 'task-1', force: true },
    { action: 'resume', task_id: 'task-1', replay: true },
  ]) assert.equal(taskRequestSchema.safeParse(request).success, false, JSON.stringify(request));
});

test('all three MCP tools forward the shared backend actions and preserve completion state', async () => {
  const registered = new Map();
  const calls = [];
  const incomplete = { content: [{ type: 'text', text: '{"state":"incomplete","completed":false}' }] };
  registerTaskTools({ registerTool: (name, config, call) => registered.set(name, { config, call }) },
    async input => { calls.push(input); return incomplete; });
  assert.deepEqual([...registered.keys()], ['desktop_skills', 'desktop_prepare_task', 'desktop_task']);
  assert.equal(registered.get('desktop_skills').config.annotations.readOnlyHint, true);
  assert.equal(registered.get('desktop_prepare_task').config.annotations.readOnlyHint, false);
  await registered.get('desktop_skills').call({ request: { action: 'list' } });
  await registered.get('desktop_skills').call({ request: { action: 'resolve', goal: 'Fill form' } });
  await registered.get('desktop_skills').call({ request: { action: 'read', skill_id: 'builtin:fill-form' } });
  await registered.get('desktop_prepare_task').call({ goal: 'Fill form', requested_skills: ['builtin:fill-form'] });
  await registered.get('desktop_task').call({ request: { action: 'status', task_id: 'task-1' } });
  await registered.get('desktop_task').call({ request: { action: 'check', task_id: 'task-1', check_id: 'x', path: 'output.csv' } });
  await registered.get('desktop_task').call({ request: { action: 'resume', task_id: 'task-1' } });
  assert.equal(await registered.get('desktop_task').call({ request: { action: 'finish', task_id: 'task-1' } }), incomplete);
  assert.deepEqual(calls, [
    { action: 'skill_list' }, { action: 'skill_resolve', goal: 'Fill form' },
    { action: 'skill_read', skill_id: 'builtin:fill-form' },
    { action: 'task_prepare', goal: 'Fill form', requested_skills: ['builtin:fill-form'] },
    { action: 'task_status', task_id: 'task-1' },
    { action: 'task_check', task_id: 'task-1', check_id: 'x', path: 'output.csv' },
    { action: 'task_resume', task_id: 'task-1' }, { action: 'task_finish', task_id: 'task-1' },
  ]);
  await assert.rejects(() => registered.get('desktop_task').call({ request: { action: 'finish', task_id: 'task-1', force: true } }));
  assert.equal(calls.length, 8);
});
