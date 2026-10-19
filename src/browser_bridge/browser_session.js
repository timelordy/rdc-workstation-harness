'use strict';

const path = require('node:path');
const crypto = require('node:crypto');

const READ_ONLY = new Set([
  'status', 'snapshot', 'text', 'html', 'url', 'screenshot', 'tabs', 'frames',
  'inspect', 'console', 'errors', 'wait',
]);
const FRAME_ACTIONS = new Set([
  'open', 'goto', 'url', 'snapshot', 'click', 'fill', 'type', 'press', 'wait',
  'text', 'html', 'inspect', 'selectOption', 'setChecked', 'hover',
  'scrollIntoView', 'upload', 'download', 'evaluate', 'storage',
]);
const LOCATOR_KEYS = ['selector', 'role', 'label', 'placeholder', 'testid', 'text'];

function targetError(code, message, hint, count) {
  return Object.assign(new Error(message), { code, hint, count });
}

function ambiguousLocator(count) {
  return targetError('AMBIGUOUS_LOCATOR', `Ambiguous locator: matched ${count} elements`,
    'Refine the locator or explicitly set nth (zero-based).', count);
}

function stateWarning(error) {
  return { action_completed: true, state_saved: false,
    state_warning: { code: 'STATE_SAVE_FAILED', message: error?.message || String(error),
      hint: 'The action completed. Do not repeat it solely to retry saving browser state.' } };
}

function boundedInteger(value, fallback, maximum, name, minimum = 1) {
  if (value == null) return fallback;
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
  return value;
}

function hasLocator(a) {
  return LOCATOR_KEYS.some(key => typeof a[key] === 'string' && a[key].length > 0);
}

function rawLocator(scope, a) {
  if (a.selector) return scope.locator(a.selector);
  if (a.role) return scope.getByRole(a.role, { name: a.name, exact: !!a.exact });
  if (a.label) return scope.getByLabel(a.label, { exact: !!a.exact });
  if (a.placeholder) return scope.getByPlaceholder(a.placeholder, { exact: !!a.exact });
  if (a.testid) return scope.getByTestId(a.testid);
  if (a.text) return scope.getByText(a.text, { exact: !!a.exact });
  throw new Error('Locator required: selector, role/name, label, placeholder, testid, or text');
}

// Keep the locator strict after checking its count: a page changing between the
// check and the action must never turn an ambiguous match into a silent first().
async function locatorFor(scope, a, { allowMissing = false } = {}) {
  const locator = rawLocator(scope, a);
  if (a.nth != null && (!Number.isInteger(a.nth) || a.nth < 0)) {
    throw targetError('INVALID_ARGUMENT', 'nth must be a non-negative integer (zero-based)');
  }
  const target = a.nth == null ? locator : locator.nth(a.nth);
  let count = await locator.count();
  if (a.nth == null && count > 1) {
    throw ambiguousLocator(count);
  }
  if (!allowMissing && count <= (a.nth ?? 0)) {
    try {
      await target.waitFor({ state: 'attached', timeout: a.timeout ?? 10000 });
    } catch (error) {
      count = await locator.count();
      if (a.nth == null && count > 1) {
        throw ambiguousLocator(count);
      }
      if (count <= (a.nth ?? 0)) {
        throw targetError('LOCATOR_NOT_FOUND', a.nth == null ? 'Locator not found'
          : `Locator nth ${a.nth} not found (matched ${count} elements)`,
        'Refresh the snapshot or inspect the intended tab and frame.', count);
      }
      throw error;
    }
  }
  return target;
}

async function inspectElement(locator, a) {
  const maxLength = boundedInteger(a.max_length, 500, 4000, 'max_length');
  const maxOptions = boundedInteger(a.max_options, 50, 200, 'max_options');
  return locator.evaluate((element, limits) => {
    const cut = value => value == null ? null : String(value).slice(0, limits.maxLength);
    const attr = name => element.getAttribute(name);
    const tag = element.tagName.toLowerCase();
    const type = tag === 'input' ? element.type.toLowerCase() : attr('type');
    const password = tag === 'input' && type === 'password';
    const labelledBy = (attr('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => element.ownerDocument.getElementById(id)?.textContent || '').join(' ');
    const labels = element.labels ? Array.from(element.labels, label => label.textContent || '').join(' ') : '';
    // This is an inspectable label hint, not a full browser accessible-name algorithm.
    const name = attr('aria-label') || labelledBy || labels || attr('alt') || attr('title')
      || ((tag === 'button' || tag === 'a' || tag === 'option') ? element.textContent : '') || '';
    const role = attr('role') || ({ button: 'button', select: element.multiple ? 'listbox' : 'combobox',
      textarea: 'textbox', a: attr('href') ? 'link' : null,
      input: ({ checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
        range: 'slider', number: 'spinbutton', search: 'searchbox' })[type]
        || (['text', 'email', 'url', 'tel'].includes(type) ? 'textbox' : null) })[tag] || null;
    const options = tag === 'select' ? Array.from(element.options).slice(0, limits.maxOptions)
      .map(option => ({ value: cut(option.value), label: cut(option.label), selected: option.selected,
        disabled: option.disabled || !!option.closest('optgroup:disabled') })) : undefined;
    return {
      tag, role: cut(role), name: cut(name.trim()), type: cut(type),
      value: password ? '[REDACTED]' : ('value' in element ? cut(element.value) : null),
      value_redacted: password,
      checked: typeof element.checked === 'boolean' ? element.checked : attr('aria-checked'),
      disabled: element.matches(':disabled') || attr('aria-disabled') === 'true',
      readonly: !!element.readOnly || attr('aria-readonly') === 'true',
      required: !!element.required || attr('aria-required') === 'true',
      multiple: !!element.multiple,
      text: password ? '' : cut(element.innerText || element.textContent || ''),
      options,
      options_truncated: tag === 'select' && element.options.length > limits.maxOptions,
      option_count: tag === 'select' ? element.options.length : undefined,
    };
  }, { maxLength, maxOptions });
}

function createBrowserSession({ chromium, profileDir, stateFile, artifactDir,
  executablePath, channel, headed: initialHeaded = false, queueTimeoutMs = 30000 }) {
  queueTimeoutMs = boundedInteger(queueTimeoutMs, 30000, 60000, 'queueTimeoutMs');
  let browser = null;
  let context = null;
  let page = null;
  let headed = initialHeaded;
  let attached = false;
  let consoleBuffer = [];
  let errorBuffer = [];
  let queue = Promise.resolve();
  let nextTabId = 1;
  let nextFrameId = 1;
  // A watchdog can replace the whole bridge process. An old ID must not alias
  // a new page even when a brand-new session starts its counter at one again.
  const generation = crypto.randomBytes(12).toString('hex');
  const tabIds = new WeakMap();
  const frameIds = new WeakMap();
  const wiredPages = new WeakSet();
  const tabId = p => {
    if (!tabIds.has(p)) tabIds.set(p, `tab-${generation}-${nextTabId++}`);
    return tabIds.get(p);
  };
  const frameId = frame => {
    if (!frameIds.has(frame)) frameIds.set(frame, `frame-${generation}-${nextFrameId++}`);
    return frameIds.get(frame);
  };
  const artifact = name => path.join(artifactDir, name);

  function wirePage(p) {
    tabId(p);
    if (wiredPages.has(p)) return;
    wiredPages.add(p);
    p.on('console', msg => {
      consoleBuffer.push({ tab_id: tabId(p), type: msg.type(), text: msg.text(), ts: Date.now() });
      if (consoleBuffer.length > 200) consoleBuffer.shift();
    });
    p.on('pageerror', error => {
      errorBuffer.push({ tab_id: tabId(p), text: String(error), ts: Date.now() });
      if (errorBuffer.length > 100) errorBuffer.shift();
    });
  }

  async function launch() {
    if (context) return;
    if (attached) throw targetError('EXTERNAL_DISCONNECTED', 'The attached application disconnected',
      'Attach again or explicitly detach. A managed browser will not be launched as a fallback.');
    const options = { headless: !headed, acceptDownloads: true,
      viewport: { width: 1440, height: 1000 }, downloadsPath: artifactDir };
    if (executablePath) options.executablePath = executablePath;
    else if (channel) options.channel = channel;
    const launched = await chromium.launchPersistentContext(profileDir, options);
    context = launched;
    browser = context.browser();
    context.on('close', () => {
      if (context === launched) { context = null; browser = null; page = null; }
    });
    context.on('page', p => {
      wirePage(p);
      // A popup is discoverable through tabs, but cannot silently redirect a
      // caller's next action away from the explicitly selected page.
      if (!page || page.isClosed()) page = p;
    });
    for (const p of context.pages()) wirePage(p);
    page = context.pages().find(p => !p.isClosed()) || await context.newPage();
    wirePage(page);
  }

  async function saveState() {
    if (context && !attached) await context.storageState({ path: stateFile, indexedDB: true });
  }

  async function closeBrowser() {
    if (attached) {
      // Never close the borrowed default context or persist its cookies/storage.
      if (browser) await browser.close(); // CDP connection teardown, tested against an independent browser.
      context = null; browser = null; page = null; attached = false;
      consoleBuffer = []; errorBuffer = [];
      return { ok: true, detached: true, external_application_closed: false, state_saved: false };
    }
    const closingContext = context;
    const closingBrowser = browser;
    let persistence = {};
    let contextClosed = !closingContext;
    let browserClosed = false;
    const closeErrors = [];
    if (closingContext) {
      try { await saveState(); } catch (error) { persistence = stateWarning(error); }
      try { await closingContext.close(); contextClosed = true; }
      catch (error) { closeErrors.push(error); }
    }
    if (closingBrowser) {
      try { await closingBrowser.close(); browserClosed = true; }
      catch (error) { closeErrors.push(error); }
    }
    if (contextClosed || browserClosed) {
      if (context === closingContext) { context = null; browser = null; page = null; }
    } else {
      // Retain live references when neither close succeeded so a caller can
      // retry cleanup; do not orphan a browser by merely clearing variables.
      throw targetError('BROWSER_CLOSE_FAILED', 'Could not close the browser',
        'Retry close or stop the local browser process. ' + closeErrors.map(error => error.message).join('; '));
    }
    return { ok: true, stateFile, ...persistence,
      ...(closeErrors.length ? { cleanup_warning: closeErrors.map(error => error.message).join('; ') } : {}) };
  }

  function explicitPage(a, { allowIndex = false } = {}) {
    const pages = context.pages().filter(p => !p.isClosed());
    if (a.tab_id != null) {
      if (typeof a.tab_id !== 'string' || !a.tab_id) throw new Error('tab_id must be a non-empty string');
      const target = pages.find(p => tabId(p) === a.tab_id);
      if (!target) throw targetError('TAB_NOT_FOUND', `Tab not found: ${a.tab_id}`, 'Refresh tabs and use a live tab_id.');
      return target;
    }
    if (allowIndex && a.index != null) {
      if (!Number.isInteger(a.index) || a.index < 0 || !pages[a.index]) {
        throw targetError('TAB_NOT_FOUND', 'Tab index not found', 'Refresh tabs; prefer a stable tab_id over an index.');
      }
      return pages[a.index];
    }
    return null;
  }

  async function activePage(a = {}) {
    const explicit = explicitPage(a);
    if (explicit) return explicit;
    if (!page || page.isClosed()) {
      page = context.pages().find(p => !p.isClosed()) || null;
      if (!page && attached) throw targetError('NO_EXTERNAL_TAB', 'Attached application has no available page',
        'Open a page explicitly; read operations never create a page in a borrowed application.');
      if (!page) page = await context.newPage();
      wirePage(page);
    }
    return page;
  }

  async function scopeFor(p, a) {
    if (a.frame_id != null && a.frame_selector != null) throw new Error('Use frame_id or frame_selector, not both');
    if (a.frame_id != null) {
      if (typeof a.frame_id !== 'string' || !a.frame_id) throw new Error('frame_id must be a non-empty string');
      const frame = p.frames().find(frame => !frame.isDetached() && frameId(frame) === a.frame_id);
      if (!frame) throw targetError('FRAME_NOT_FOUND', `Frame not found in requested tab: ${a.frame_id}`,
        'Refresh frames for the requested tab_id.');
      return frame;
    }
    if (a.frame_selector != null) {
      const selectors = Array.isArray(a.frame_selector) ? a.frame_selector : [a.frame_selector];
      if (!selectors.length || selectors.length > 8 || selectors.some(s => typeof s !== 'string' || !s.trim())) {
        throw new Error('frame_selector must be a non-empty selector or an array of 1 to 8 selectors');
      }
      let scope = p;
      for (const selector of selectors) {
        const locator = await locatorFor(scope, { selector, timeout: a.timeout });
        const handle = await locator.elementHandle({ timeout: a.timeout ?? 10000 });
        try {
          const frame = handle && await handle.contentFrame();
          if (!frame || frame.isDetached()) throw new Error('frame_selector must resolve to an attached iframe/frame');
          scope = frame;
        } finally {
          if (handle) await handle.dispose();
        }
      }
      return scope;
    }
    return p;
  }

  async function attachBrowser(a) {
    if (context) throw targetError('SESSION_BUSY', 'A browser session is already active',
      'Close a managed session or detach a borrowed one explicitly before attaching.');
    let endpoint;
    try { endpoint = new URL(a.endpoint); } catch { throw new Error('endpoint must be a loopback CDP WebSocket URL'); }
    if (endpoint.protocol !== 'ws:' || !['127.0.0.1', '[::1]'].includes(endpoint.hostname)
        || !endpoint.port || endpoint.port === '0' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
        || !/^\/devtools\/browser\/[A-Za-z0-9-]+$/.test(endpoint.pathname)) {
      throw new Error('Use ws://127.0.0.1:<port>/devtools/browser/<id> (or [::1]); remote hosts, credentials and HTTP redirects are not accepted');
    }
    const timeout = boundedInteger(a.timeout, 10000, 30000, 'timeout');
    const remote = await chromium.connectOverCDP(endpoint.href, { timeout, noDefaults: true });
    const existing = remote.contexts()[0];
    if (!existing) { await remote.close(); throw new Error('External application exposes no browser context'); }
    browser = remote; context = existing; attached = true;
    page = context.pages().find(p => !p.isClosed()) || null;
    for (const p of context.pages()) wirePage(p);
    context.on('page', p => { wirePage(p); if (!page || page.isClosed()) page = p; });
    remote.on('disconnected', () => {
      if (browser === remote) { context = null; browser = null; page = null; }
    });
    return { ok: true, ownership: 'attached', pages: context.pages().length, state_persistence: false,
      hint: 'Use tabs and explicit tab_id. detach/close disconnect without closing the external application.' };
  }

  async function handle(a) {
    if (!a || typeof a.action !== 'string' || !a.action) throw new Error('Missing action');
    const hasFrame = a.frame_id != null || a.frame_selector != null;
    if (hasFrame && !FRAME_ACTIONS.has(a.action)) throw new Error(`Action ${a.action} does not support frame targeting`);
    if (a.action === 'attach') return attachBrowser(a);
    if (a.action === 'detach') {
      if (!attached) return { ok: true, detached: false };
      return closeBrowser();
    }
    if (a.action === 'status') return { running: !!context, headed, profileDir: attached ? null : profileDir, artifactDir,
      ownership: attached ? 'attached' : 'managed', state_persistence: !attached,
      pages: context ? context.pages().length : 0 };
    if (a.action === 'close') return closeBrowser();
    if (a.action === 'restart') {
      if (attached) throw new Error('restart is not allowed for an attached application; detach explicitly first');
      const closed = await closeBrowser();
      if (typeof a.headed === 'boolean') headed = a.headed;
      await launch();
      return { ok: true, headed, stateFile, tab_id: tabId(page),
        ...(closed.state_warning ? { previous_state_warning: closed.state_warning } : {}) };
    }
    await launch();
    if (a.action === 'tabs') return {
      tabs: await Promise.all(context.pages().filter(p => !p.isClosed()).map(async (p, index) => ({
        index, tab_id: tabId(p), url: p.url(), title: await p.title(), active: p === page,
      }))),
    };
    if (a.action === 'newTab') {
      page = await context.newPage();
      wirePage(page);
      if (a.url) await page.goto(a.url, { waitUntil: 'domcontentloaded', timeout: a.timeout ?? 30000 });
      return { ok: true, index: context.pages().indexOf(page), tab_id: tabId(page), url: page.url() };
    }
    if (a.action === 'useTab') {
      const target = explicitPage(a, { allowIndex: true });
      if (!target) throw new Error('useTab requires tab_id or index');
      await target.bringToFront();
      page = target;
      return { ok: true, index: context.pages().indexOf(page), tab_id: tabId(page), url: page.url() };
    }
    if (a.action === 'closeTab') {
      const target = explicitPage(a, { allowIndex: true }) || await activePage();
      const closedId = tabId(target);
      await target.close();
      if (page === target) page = context.pages().find(p => !p.isClosed()) || null;
      return { ok: true, tab_id: closedId };
    }
    if (a.action === 'cookies') {
      if (a.clear) { await context.clearCookies(); return { ok: true }; }
      return { cookies: await context.cookies(a.urls || undefined) };
    }
    if (a.action === 'console' || a.action === 'errors') {
      if (a.tab_id != null) explicitPage(a);
      const limit = boundedInteger(a.limit, 50, 200, 'limit');
      const buffer = a.action === 'console' ? consoleBuffer : errorBuffer;
      const items = buffer.filter(item => a.tab_id == null || item.tab_id === a.tab_id).slice(-limit);
      if (a.clear) {
        const remaining = a.tab_id == null ? [] : buffer.filter(item => item.tab_id !== a.tab_id);
        if (a.action === 'console') consoleBuffer = remaining;
        else errorBuffer = remaining;
      }
      return { items };
    }
    const p = await activePage(a);
    if (a.action === 'frames') {
      const maxFrames = boundedInteger(a.max_frames, 100, 200, 'max_frames');
      const frames = p.frames().filter(frame => !frame.isDetached());
      return { tab_id: tabId(p), frames: frames.slice(0, maxFrames).map(frame => ({
        frame_id: frameId(frame), parent_frame_id: frame.parentFrame() ? frameId(frame.parentFrame()) : null,
        name: frame.name(), url: frame.url(), is_main: frame === p.mainFrame(),
      })), truncated: frames.length > maxFrames };
    }
    const scope = await scopeFor(p, a);
    return { tab_id: tabId(p), frame_id: frameId(scope === p ? p.mainFrame() : scope),
      ...await handlePage(p, scope, a) };
  }

  async function handlePage(p, scope, a) {
    const timeout = a.timeout ?? 10000;
    const locate = options => locatorFor(scope, a, options);
    if (a.action === 'open' || a.action === 'goto') {
      await scope.goto(a.url, { waitUntil: a.waitUntil || 'domcontentloaded', timeout: a.timeout ?? 30000 });
      return { ok: true, url: scope.url(), title: await scope.title() };
    }
    if (a.action === 'snapshot') {
      const body = scope.locator('body');
      const aria = await body.ariaSnapshot({ timeout });
      return { url: scope.url(), title: await scope.title(), aria };
    }
    if (a.action === 'click') {
      await (await locate()).click({ timeout });
      return { ok: true, url: p.url() };
    }
    if (a.action === 'fill') {
      await (await locate()).fill(a.value ?? '', { timeout });
      return { ok: true };
    }
    if (a.action === 'type') {
      await (await locate()).pressSequentially(a.value ?? '', { delay: a.delay || 0, timeout });
      return { ok: true };
    }
    if (a.action === 'press') {
      const target = hasLocator(a) ? await locate() : scope.locator('body');
      await target.press(a.key, { timeout });
      return { ok: true };
    }
    if (a.action === 'wait') {
      if (a.ms != null) await p.waitForTimeout(a.ms);
      else await (await locate({ allowMissing: ['hidden', 'detached'].includes(a.state) }))
        .waitFor({ state: a.state || 'visible', timeout });
      return { ok: true };
    }
    if (a.action === 'inspect') return { element: await inspectElement(await locate(), a) };
    if (a.action === 'selectOption') {
      if (!(typeof a.value === 'string' || (Array.isArray(a.value) && a.value.every(v => typeof v === 'string')))) {
        throw new Error('selectOption requires value: an option value string or array of strings');
      }
      return { ok: true, values: await (await locate()).selectOption(a.value, { timeout }) };
    }
    if (a.action === 'setChecked') {
      if (typeof a.checked !== 'boolean') throw new Error('setChecked requires checked: true or false');
      await (await locate()).setChecked(a.checked, { timeout });
      return { ok: true, checked: a.checked };
    }
    if (a.action === 'hover') { await (await locate()).hover({ timeout }); return { ok: true }; }
    if (a.action === 'scrollIntoView') { await (await locate()).scrollIntoViewIfNeeded({ timeout }); return { ok: true }; }
    if (a.action === 'text') return { text: await (await locate()).innerText({ timeout }) };
    if (a.action === 'html') return { html: await (await locate()).innerHTML({ timeout }) };
    if (a.action === 'url') return { url: scope.url(), title: await scope.title() };
    if (a.action === 'screenshot') {
      const file = a.path || artifact(`shot-${Date.now()}.png`);
      await p.screenshot({ path: file, fullPage: a.fullPage !== false });
      return { ok: true, path: file };
    }
    if (a.action === 'upload') {
      const files = Array.isArray(a.files) ? a.files : [a.path];
      await (await locate()).setInputFiles(files, { timeout });
      return { ok: true, files };
    }
    if (a.action === 'download') {
      const target = await locate();
      const [download] = await Promise.all([
        p.waitForEvent('download', { timeout: a.timeout ?? 30000 }), target.click({ timeout }),
      ]);
      const suggested = download.suggestedFilename();
      const file = a.path || artifact(suggested);
      await download.saveAs(file);
      return { ok: true, path: file, suggestedFilename: suggested };
    }
    if (a.action === 'evaluate') {
      const result = await scope.evaluate(({ expression, arg }) => {
        const fn = new Function('arg', `return (${expression});`);
        return fn(arg);
      }, { expression: a.expression, arg: a.arg });
      return { result };
    }
    if (a.action === 'storage') {
      const kind = a.kind === 'session' ? 'sessionStorage' : 'localStorage';
      if (a.clear) { await scope.evaluate(k => window[k].clear(), kind); return { ok: true }; }
      if (a.set) {
        await scope.evaluate(({ k, values }) => {
          for (const [key, value] of Object.entries(values)) window[k].setItem(key, String(value));
        }, { k: kind, values: a.set });
        return { ok: true };
      }
      return { values: await scope.evaluate(k => Object.fromEntries(Object.entries(window[k])), kind) };
    }
    if (['back', 'forward', 'reload'].includes(a.action)) {
      const method = { back: 'goBack', forward: 'goForward', reload: 'reload' }[a.action];
      await p[method]({ waitUntil: 'domcontentloaded' });
      return { ok: true, url: p.url() };
    }
    throw new Error(`Unknown action: ${a.action}`);
  }

  function enqueue(operation, { signal, timeoutMs = queueTimeoutMs } = {}) {
    let waiting = true;
    let cancellation = null;
    let timer;
    let rejectWaiting;
    const deadline = timeoutMs ? Date.now() + timeoutMs : null;
    const cancelled = new Promise((resolve, reject) => { rejectWaiting = reject; });
    const cleanup = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); };
    const cancel = (code, message) => {
      if (!waiting || cancellation) return;
      cancellation = Object.assign(targetError(code, message,
        'The request was not started. Inspect the current tab state before submitting a new request.'), { action_started: false });
      cleanup();
      rejectWaiting(cancellation);
    };
    const abort = () => cancel('REQUEST_CANCELLED', 'Browser request cancelled before execution');
    if (timeoutMs) timer = setTimeout(() => cancel('QUEUE_TIMEOUT', 'Browser request expired before execution'), timeoutMs);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    const work = queue.then(async () => {
      // Check the deadline as well as the timer: a busy event loop may run this
      // promise callback before it services an already-due timeout callback.
      if (deadline && Date.now() >= deadline) cancel('QUEUE_TIMEOUT', 'Browser request expired before execution');
      if (cancellation) throw cancellation;
      waiting = false;
      cleanup();
      return operation();
    });
    // Queue continuity follows actual work, not the response race. An expired
    // request can return immediately but must never let work overlap its predecessor.
    queue = work.catch(() => undefined);
    return Promise.race([work, cancelled]);
  }

  return {
    run(a, options = {}) {
      return enqueue(async () => {
        const result = await handle(a);
        const mutates = !READ_ONLY.has(a.action)
          && !(a.action === 'cookies' && !a.clear)
          && !(a.action === 'storage' && !a.clear && !a.set);
        if (context && mutates) {
          try { await saveState(); }
          catch (error) { return { ...result, ...stateWarning(error) }; }
        }
        return result;
      }, options);
    },
    shutdown: () => enqueue(closeBrowser, { timeoutMs: 0 }),
    get headed() { return headed; },
  };
}

module.exports = { createBrowserSession, locatorFor, inspectElement };
