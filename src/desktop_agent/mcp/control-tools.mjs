import { z } from 'zod';

// The advanced desktop_action escape hatch remains available. These two tools
// make everyday structured browser/UIA control discoverable without a generic
// action dictionary or a long list of one-tool-per-click registrations.
export const BROWSER_ACTION_NAMES = [
  'status', 'restart', 'open', 'goto', 'snapshot', 'frames', 'inspect',
  'click', 'fill', 'type', 'selectOption', 'setChecked', 'hover', 'scrollIntoView',
  'press', 'wait', 'text', 'html', 'url', 'screenshot', 'upload', 'download',
  'evaluate', 'cookies', 'storage', 'tabs', 'newTab', 'useTab', 'closeTab',
  'console', 'errors', 'back', 'forward', 'reload', 'close',
];

const locatorKeys = ['selector', 'role', 'label', 'placeholder', 'testid', 'text'];
const locatorActions = new Set([
  'inspect', 'click', 'fill', 'type', 'selectOption', 'setChecked', 'hover',
  'scrollIntoView', 'text', 'html', 'upload', 'download',
]);

export const browserRequestSchema = z.object({
  action: z.enum(BROWSER_ACTION_NAMES),
  tab_id: z.string().min(1).optional().describe('Stable live tab ID from tabs/newTab. Prefer over current tab.'),
  frame_id: z.string().min(1).optional().describe('Live frame ID from frames; alternative to frame_selector.'),
  frame_selector: z.union([z.string().min(1), z.array(z.string().min(1)).min(1).max(8)])
    .optional().describe('Iframe CSS selector, or a chain of nested iframe selectors.'),
  selector: z.string().min(1).optional(),
  role: z.string().min(1).optional(),
  name: z.string().optional(),
  label: z.string().min(1).optional(),
  placeholder: z.string().min(1).optional(),
  testid: z.string().min(1).optional(),
  text: z.string().min(1).optional(),
  exact: z.boolean().optional(),
  nth: z.number().int().min(0).optional().describe('Explicit zero-based match. Omit to require a unique match.'),
  url: z.string().min(1).optional(),
  value: z.union([z.string(), z.array(z.string())]).optional(),
  checked: z.boolean().optional(),
  key: z.string().min(1).optional(),
  timeout: z.number().int().positive().max(3600000).optional().describe('Milliseconds, unlike desktop action timeouts.'),
  index: z.number().int().min(0).optional().describe('Legacy tab index for useTab/closeTab.'),
}).passthrough().superRefine((request, ctx) => {
  const problem = (path, message) => ctx.addIssue({ code: 'custom', path: [path], message });
  const count = locatorKeys.filter(key => request[key] !== undefined).length;
  if (count > 1) problem('selector', 'Use exactly one locator strategy (role may include name).');
  if (locatorActions.has(request.action) && count !== 1) problem('selector', 'This action requires one locator.');
  if (request.frame_id && request.frame_selector) problem('frame_id', 'Use frame_id or frame_selector, not both.');
  if (['open', 'goto'].includes(request.action) && !request.url) problem('url', 'Navigation requires url.');
  if (['fill', 'type'].includes(request.action) && typeof request.value !== 'string') problem('value', 'Text input requires a string value.');
  if (request.action === 'selectOption' && request.value === undefined) problem('value', 'selectOption requires option values.');
  if (request.action === 'setChecked' && typeof request.checked !== 'boolean') problem('checked', 'setChecked requires a boolean.');
  if (request.action === 'press' && !request.key) problem('key', 'press requires key.');
});

export function registerControlTools(server, callAction) {
  server.registerTool('desktop_browser', {
    title: 'Control Browser Through DOM',
    description: 'Read and operate the managed workstation browser through Playwright. Start with tabs, '
      + 'then use tab_id on each request; snapshot and frames reveal the page structure, inspect reads '
      + 'element state, fill/selectOption/setChecked complete forms. Locators must match uniquely unless '
      + 'nth is explicit. Use desktop_describe {browser: action} for additional parameters. '
      + 'Uses the harness browser profile; attaching ordinary existing browser sessions is not supported here. '
      + 'Set commit only when the user has authorized the external action; it forwards the existing agent gate.',
    inputSchema: z.object({
      request: browserRequestSchema,
      commit: z.boolean().optional().describe('Existing authorization for an externally visible action (send/pay/delete/etc.).'),
    }),
  }, async ({ request, commit }) => callAction({ action: 'browser', payload: request, commit: commit === true }));

  server.registerTool('desktop_inspect', {
    title: 'Inspect Windows Control State',
    description: 'Read a Windows control through UI Automation: supported patterns, text/value, checked, '
      + 'selected and expanded state. Password values are redacted. Discover the target with windows/uia_tree; '
      + 'use this before a semantic action and to verify its result. No mouse or keyboard input.',
    inputSchema: z.object({
      window: z.record(z.string(), z.any()).describe('Top-level UIA selector, e.g. {title_re:"Notepad",pid:1234}.'),
      control: z.record(z.string(), z.any()).optional().describe('Descendant selector, e.g. {automation_id:"includeLinks"}.'),
      max_text: z.number().int().min(0).max(20000).optional().describe('Maximum characters per value/text field; default 4000.'),
    }),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, async (args) => callAction({ action: 'uia_inspect', ...args }));
}
