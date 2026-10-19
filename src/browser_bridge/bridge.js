const { chromium } = require('playwright');
const readline = require('readline');
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { createBrowserSession } = require('./browser_session');

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

const session = createBrowserSession({
  chromium,
  profileDir: PROFILE_DIR,
  stateFile: STATE_FILE,
  artifactDir: ARTIFACT_DIR,
  executablePath: BROWSER_EXE,
  channel: process.env.PW_CHANNEL,
  headed: process.env.PW_HEADED === '1',
});

function out(value) {
  process.stdout.write(JSON.stringify(value) + '\n');
}

function errorResult(error) {
  return {
    ok: false,
    error: error?.message || String(error),
    code: typeof error?.code === 'string' ? error.code : undefined,
    hint: typeof error?.hint === 'string' ? error.hint : undefined,
    count: Number.isInteger(error?.count) ? error.count : undefined,
    action_started: typeof error?.action_started === 'boolean' ? error.action_started : undefined,
  };
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
    const abort = new AbortController();
    const cancelQueued = () => { if (!res.writableEnded) abort.abort(); };
    req.once('aborted', cancelQueued);
    res.once('close', cancelQueued);
    try {
      const action = JSON.parse(body || '{}');
      const result = await session.run(action, { signal: abort.signal });
      res.end(JSON.stringify({ ok: true, ...result }));
    } catch (error) {
      res.statusCode = 500;
      res.end(JSON.stringify(errorResult(error)));
    } finally {
      req.removeListener('aborted', cancelQueued);
      res.removeListener('close', cancelQueued);
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
  headed: session.headed
});

rl.on('line', async line => {
  const raw = line.trim();
  if (!raw) return;
  try {
    const action = JSON.parse(raw);
    const result = await session.run(action);
    out({ ok: true, ...result });
  } catch (error) {
    out({
      ...errorResult(error),
      stack: process.env.PW_DEBUG === '1' && error ? error.stack : undefined
    });
  }
});

async function shutdown() {
  try {
    await session.shutdown();
  } finally {
    process.exit(0);
  }
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
