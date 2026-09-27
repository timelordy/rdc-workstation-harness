const { chromium } = require('playwright');
const readline = require('readline');
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const HOME = process.env.USERPROFILE || process.env.HOME;
const TOKEN_DIR = process.env.RDC_HARNESS_TOKEN_DIR || path.join(HOME, '.chatgpt-desktop-agent');
const PROFILE_DIR = process.env.PW_PROFILE_DIR || path.join(HOME, '.rdc-workstation-harness', 'browser-profile');
const STATE_FILE = path.join(PROFILE_DIR, 'storage-state.json');
const ARTIFACT_DIR = process.env.PW_ARTIFACT_DIR || path.join(HOME, 'Downloads', 'rdc-harness-browser');
const TOKEN_FILE = path.join(TOKEN_DIR, 'browser.token');
const LOCAL_APP_DATA = process.env.LOCALAPPDATA || path.join(HOME, 'AppData', 'Local');
const PROGRAM_FILES = process.env.PROGRAMFILES || String.raw`C:\Program Files`;
const PROGRAM_FILES_X86 = process.env['PROGRAMFILES(X86)'] || String.raw`C:\Program Files (x86)`;
const browserCandidates = [
  path.join(LOCAL_APP_DATA, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
  path.join(PROGRAM_FILES, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
  path.join(PROGRAM_FILES_X86, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
  path.join(LOCAL_APP_DATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(PROGRAM_FILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(PROGRAM_FILES_X86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(PROGRAM_FILES, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  path.join(PROGRAM_FILES_X86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
];
const BROWSER_EXE = process.env.PW_EXECUTABLE || browserCandidates.find(candidate => fs.existsSync(candidate));
fs.mkdirSync(PROFILE_DIR, { recursive: true });
fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
fs.mkdirSync(TOKEN_DIR, { recursive: true });
let BROWSER_TOKEN;
if (process.env.PW_BRIDGE_TOKEN) {
  BROWSER_TOKEN = process.env.PW_BRIDGE_TOKEN.trim();
} else if (fs.existsSync(TOKEN_FILE)) {
  BROWSER_TOKEN = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
} else {
  BROWSER_TOKEN = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(TOKEN_FILE, BROWSER_TOKEN, 'utf8');
}

function tokenMatches(value) {
  if (typeof value !== 'string') return false;
  const a = Buffer.from(value);
  const b = Buffer.from(BROWSER_TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

let browser = null;
let context = null;
let page = null;
let headed = process.env.PW_HEADED === '1';
let consoleBuffer = [];
let errorBuffer = [];

function out(value) {
  process.stdout.write(JSON.stringify(value) + '\n');
}

function artifact(name) {
  return path.join(ARTIFACT_DIR, name);
}

function wirePage(p) {
  p.on('console', msg => {
    consoleBuffer.push({ type: msg.type(), text: msg.text(), ts: Date.now() });
    if (consoleBuffer.length > 200) consoleBuffer.shift();
  });
  p.on('pageerror', err => {
    errorBuffer.push({ text: String(err), ts: Date.now() });
    if (errorBuffer.length > 100) errorBuffer.shift();
  });
}

async function launch() {
  if (context) return;
  const options = {
    headless: !headed,
    acceptDownloads: true,
    viewport: { width: 1440, height: 1000 },
    downloadsPath: ARTIFACT_DIR
  };
  if (BROWSER_EXE) options.executablePath = BROWSER_EXE;
  else if (process.env.PW_CHANNEL) options.channel = process.env.PW_CHANNEL;
  context = await chromium.launchPersistentContext(PROFILE_DIR, options);
  browser = context.browser();
  page = context.pages().find(p => !p.isClosed()) || await context.newPage();
  wirePage(page);
  context.on('page', p => { wirePage(p); page = p; });
}

async function saveState() {
  if (!context) return;
  await context.storageState({ path: STATE_FILE, indexedDB: true });
}
async function activePage() {
  await launch();
  if (!page || page.isClosed()) {
    page = context.pages().find(p => !p.isClosed()) || await context.newPage();
    wirePage(page);
  }
  return page;
}

function locatorFor(p, a) {
  if (a.selector) return p.locator(a.selector).first();
  if (a.role) return p.getByRole(a.role, { name: a.name, exact: !!a.exact }).first();
  if (a.label) return p.getByLabel(a.label, { exact: !!a.exact }).first();
  if (a.placeholder) return p.getByPlaceholder(a.placeholder, { exact: !!a.exact }).first();
  if (a.testid) return p.getByTestId(a.testid).first();
  if (a.text) return p.getByText(a.text, { exact: !!a.exact }).first();
  throw new Error('Locator required: selector, role/name, label, placeholder, testid, or text');
}

async function handle(a) {
  if (!a || !a.action) throw new Error('Missing action');
  if (a.action === 'status') {
    return {
      running: !!context,
      headed,
      profileDir: PROFILE_DIR,
      artifactDir: ARTIFACT_DIR,
      pages: context ? context.pages().length : 0
    };
  }

  if (a.action === 'restart') {
    if (context) await saveState();
    if (context) await context.close();
    if (browser) await browser.close();
    browser = null;
    context = null;
    page = null;
    headed = !!a.headed;
    await launch();
    return { ok: true, headed, stateFile: STATE_FILE };
  }

  const p = await activePage();

  if (a.action === 'open' || a.action === 'goto') {
    await p.goto(a.url, { waitUntil: a.waitUntil || 'domcontentloaded', timeout: a.timeout || 30000 });
    return { ok: true, url: p.url(), title: await p.title() };
  }
  if (a.action === 'snapshot') {
    const body = p.locator('body');
    const aria = await body.ariaSnapshot({ timeout: a.timeout || 10000 });
    return { url: p.url(), title: await p.title(), aria };
  }

  if (a.action === 'click') {
    await locatorFor(p, a).click({ timeout: a.timeout || 10000 });
    return { ok: true, url: p.url() };
  }

  if (a.action === 'fill') {
    await locatorFor(p, a).fill(a.value ?? '', { timeout: a.timeout || 10000 });
    return { ok: true };
  }

  if (a.action === 'type') {
    await locatorFor(p, a).pressSequentially(a.value ?? '', { delay: a.delay || 0, timeout: a.timeout || 10000 });
    return { ok: true };
  }

  if (a.action === 'press') {
    const target = a.selector || a.role || a.label || a.placeholder || a.testid || a.text
      ? locatorFor(p, a)
      : p.locator('body');
    await target.press(a.key, { timeout: a.timeout || 10000 });
    return { ok: true };
  }

  if (a.action === 'wait') {
    if (a.ms != null) await p.waitForTimeout(a.ms);
    else await locatorFor(p, a).waitFor({ state: a.state || 'visible', timeout: a.timeout || 10000 });
    return { ok: true };
  }

  if (a.action === 'text') {
    const text = await locatorFor(p, a).innerText({ timeout: a.timeout || 10000 });
    return { text };
  }

  if (a.action === 'html') {
    const html = await locatorFor(p, a).innerHTML({ timeout: a.timeout || 10000 });
    return { html };
  }

  if (a.action === 'url') return { url: p.url(), title: await p.title() };

  if (a.action === 'screenshot') {
    const file = a.path || artifact(`shot-${Date.now()}.png`);
    await p.screenshot({ path: file, fullPage: a.fullPage !== false });
    return { ok: true, path: file };
  }
  if (a.action === 'upload') {
    const files = Array.isArray(a.files) ? a.files : [a.path];
    await locatorFor(p, a).setInputFiles(files, { timeout: a.timeout || 10000 });
    return { ok: true, files };
  }

  if (a.action === 'download') {
    const [download] = await Promise.all([
      p.waitForEvent('download', { timeout: a.timeout || 30000 }),
      locatorFor(p, a).click({ timeout: a.timeout || 10000 })
    ]);
    const suggested = download.suggestedFilename();
    const file = a.path || artifact(suggested);
    await download.saveAs(file);
    return { ok: true, path: file, suggestedFilename: suggested };
  }

  if (a.action === 'evaluate') {
    const result = await p.evaluate(({ expression, arg }) => {
      const fn = new Function('arg', `return (${expression});`);
      return fn(arg);
    }, { expression: a.expression, arg: a.arg });
    return { result };
  }

  if (a.action === 'cookies') {
    if (a.clear) {
      await context.clearCookies();
      return { ok: true };
    }
    return { cookies: await context.cookies(a.urls || undefined) };
  }

  if (a.action === 'storage') {
    const kind = a.kind === 'session' ? 'sessionStorage' : 'localStorage';
    if (a.clear) {
      await p.evaluate(k => window[k].clear(), kind);
      return { ok: true };
    }
    if (a.set) {
      await p.evaluate(({ k, values }) => {
        for (const [key, value] of Object.entries(values)) window[k].setItem(key, String(value));
      }, { k: kind, values: a.set });
      return { ok: true };
    }
    const values = await p.evaluate(k => Object.fromEntries(Object.entries(window[k])), kind);
    return { values };
  }

  if (a.action === 'tabs') {
    return {
      tabs: await Promise.all(context.pages().map(async (x, i) => ({
        index: i, url: x.url(), title: await x.title()
      })))
    };
  }
  if (a.action === 'newTab') {
    page = await context.newPage();
    wirePage(page);
    if (a.url) await page.goto(a.url, { waitUntil: 'domcontentloaded' });
    return { ok: true, index: context.pages().indexOf(page), url: page.url() };
  }

  if (a.action === 'useTab') {
    const pages = context.pages();
    if (!pages[a.index]) throw new Error('Tab index not found');
    page = pages[a.index];
    await page.bringToFront();
    return { ok: true, index: a.index, url: page.url() };
  }

  if (a.action === 'closeTab') {
    const pages = context.pages();
    const target = pages[a.index ?? pages.indexOf(p)];
    if (!target) throw new Error('Tab not found');
    await target.close();
    page = context.pages()[0] || null;
    return { ok: true };
  }

  if (a.action === 'console') {
    const items = consoleBuffer.slice(-(a.limit || 50));
    if (a.clear) consoleBuffer = [];
    return { items };
  }

  if (a.action === 'errors') {
    const items = errorBuffer.slice(-(a.limit || 50));
    if (a.clear) errorBuffer = [];
    return { items };
  }

  if (a.action === 'back') {
    await p.goBack({ waitUntil: 'domcontentloaded' });
    return { ok: true, url: p.url() };
  }

  if (a.action === 'forward') {
    await p.goForward({ waitUntil: 'domcontentloaded' });
    return { ok: true, url: p.url() };
  }

  if (a.action === 'reload') {
    await p.reload({ waitUntil: 'domcontentloaded' });
    return { ok: true, url: p.url() };
  }

  if (a.action === 'close') {
    if (context) await saveState();
    if (context) await context.close();
    if (browser) await browser.close();
    browser = null;
    context = null;
    page = null;
    return { ok: true, stateFile: STATE_FILE };
  }

  throw new Error(`Unknown action: ${a.action}`);
}
const PORT = Number(process.env.PW_BRIDGE_PORT || 17321);
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  if (req.method === 'GET' && req.url === '/health') {
    res.end(JSON.stringify({ ok: true, ready: true, pid: process.pid, port: PORT }));
    return;
  }
  if (req.method !== 'POST' || req.url !== '/action') {
    res.statusCode = 404;
    res.end(JSON.stringify({ ok: false, error: 'Not found' }));
    return;
  }
  if (!tokenMatches(req.headers['x-desktop-agent-token'])) {
    res.statusCode = 403;
    res.end(JSON.stringify({ ok: false, error: 'Invalid token' }));
    return;
  }
  let body = '';
  let rejected = false;
  let received = 0;
  req.on('data', chunk => {
    if (rejected) return;
    received += chunk.length;
    if (received > 1024 * 1024) {
      rejected = true;
      res.statusCode = 413;
      res.end(JSON.stringify({ ok: false, error: 'Payload too large' }));
      return;
    }
    body += chunk;
  });
  req.on('end', async () => {
    if (rejected) return;
    try {
      const action = JSON.parse(body || '{}');
      const result = await handle(action);
      if (context) await saveState();
      res.end(JSON.stringify({ ok: true, ...result }));
    } catch (error) {
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: error?.message || String(error) }));
    }
  });
});

server.on('error', error => {
  if (error && error.code === 'EADDRINUSE') process.exit(0);
  out({ http: false, error: error.message });
});
server.listen(PORT, '127.0.0.1', () => out({ http: true, port: PORT }));

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

out({
  ready: true,
  pid: process.pid,
  profileDir: PROFILE_DIR,
  artifactDir: ARTIFACT_DIR,
  headed
});

rl.on('line', async line => {
  const raw = line.trim();
  if (!raw) return;
  try {
    const action = JSON.parse(raw);
    const result = await handle(action);
    if (context) await saveState();
    out({ ok: true, ...result });
  } catch (error) {
    out({
      ok: false,
      error: error && error.message ? error.message : String(error),
      stack: process.env.PW_DEBUG === '1' && error ? error.stack : undefined
    });
  }
});

async function shutdown() {
  try {
    if (context) await saveState();
    if (context) await context.close();
    if (browser) await browser.close();
  } finally {
    process.exit(0);
  }
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
