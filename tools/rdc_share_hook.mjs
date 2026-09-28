// Share hook for the Remote Desktop Commander device agent.
//
// Load it into the agent process, no changes to the package itself:
//   node --import file:///.../rdc_share_hook.mjs <desktop-commander>/dist/index.js remote
//
// Why: web chat clients reach this PC through the hosted Remote Desktop
// Commander. ChatGPT shows the model an empty result for image blocks, so a
// natively taken screenshot opened with read_file never appears in the chat.
// The hosted tool list is fixed, so a new tool cannot be added; the result of
// read_file, however, is produced on this PC. For remote read_file calls on
// images and documents this hook publishes the file through `pc-agent share`
// (the uploader in RDC_HARNESS_SHARE_CMD) and adds the link to the result as
// text the model can pass to the user. Unless the client is known to render
// tool images (Claude), the image block is removed so the text is not
// swallowed with it. Without RDC_HARNESS_SHARE_CMD nothing changes.
//
// Fails open: any error leaves the original result untouched.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require_ = createRequire(import.meta.url);

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOME = process.env.USERPROFILE || os.homedir();
const LOG = process.env.RDC_HARNESS_SHARE_HOOK_LOG
  || path.join(HOME, '.rdc-workstation-harness', 'logs', 'share-hook.ndjson');

export const IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']);
export const DOCUMENT_EXT = new Set([
  '.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.dwg', '.dxf', '.ifc', '.zip', '.7z',
]);
const MAX_BYTES = 200 * 1024 * 1024;
const cache = new Map(); // `${file}|${mtime}|${size}` -> url

function log(event) {
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    fs.appendFileSync(LOG, JSON.stringify({ ts: new Date().toISOString(), ...event }) + '\n');
  } catch { /* logging must never break a tool call */ }
}

function pythonExe() {
  if (process.env.RDC_HARNESS_PYTHON) return process.env.RDC_HARNESS_PYTHON;
  const venv = path.join(HERE, '..', '.venv', 'Scripts', 'python.exe');
  return fs.existsSync(venv) ? venv : 'python';
}

function pcAgentScript() {
  return process.env.RDC_HARNESS_PC_AGENT || path.join(HERE, 'pc_agent.py');
}

/** Publish one file through `pc-agent share`; resolves to the URL. */
export function shareFile(file, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonExe(), [pcAgentScript(), 'share', file], {
      windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1' },
    });
    let out = '';
    let err = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('share timed out')); }, timeoutMs);
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; });
    child.on('error', (e) => { clearTimeout(timer); reject(e); });
    child.on('close', () => {
      clearTimeout(timer);
      try {
        const res = JSON.parse(out);
        if (res.ok && res.url) resolve(res.url);
        else reject(new Error(res.error || 'share failed'));
      } catch {
        reject(new Error(`share output unreadable: ${(err || out).slice(-300)}`));
      }
    });
  });
}

function resolveLocalPath(p) {
  if (typeof p !== 'string' || !p) return null;
  const expanded = p.startsWith('~') ? path.join(HOME, p.slice(1)) : p;
  return path.resolve(expanded);
}

/** Clients known to show tool images to the model; everyone else gets the link only. */
export function keepsImages(metadata) {
  const name = metadata?.clientInfo?.name || '';
  return /claude|anthropic/i.test(name);
}

/**
 * Add a share link to a remote read_file result. Returns a new result, or the
 * original one when the call is not eligible.
 */
export function shareConfigured() {
  if (process.env.RDC_HARNESS_SHARE_CMD) return true;
  if (process.platform !== 'win32') return false;
  // A long-running agent may predate the variable: read the user registry too.
  try {
    const { execFileSync } = require_('node:child_process');
    const out = execFileSync('reg', ['query', 'HKCU\\Environment', '/v', 'RDC_HARNESS_SHARE_CMD'],
      { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] }).toString();
    return /RDC_HARNESS_SHARE_CMD\s+REG_\w+\s+\S/.test(out);
  } catch {
    return false;
  }
}

export async function augmentResult(toolName, args, metadata, result, share = shareFile, configured = shareConfigured) {
  if (toolName !== 'read_file' || !result || result.isError || args?.isUrl) return result;
  if (!configured()) return result;
  const file = resolveLocalPath(args?.path);
  if (!file) return result;
  const ext = path.extname(file).toLowerCase();
  const isImage = IMAGE_EXT.has(ext);
  if (!isImage && !DOCUMENT_EXT.has(ext)) return result;

  let stat;
  try { stat = fs.statSync(file); } catch { return result; }
  if (!stat.isFile() || stat.size > MAX_BYTES) return result;

  const client = metadata?.clientInfo?.name || 'unknown';
  log({ event: 'read_file', client, ext, bytes: stat.size });
  const key = `${file}|${stat.mtimeMs}|${stat.size}`;
  let url = cache.get(key);
  if (!url) {
    try {
      url = await share(file);
      cache.set(key, url);
      log({ event: 'shared', client, ext, bytes: stat.size });
    } catch (e) {
      log({ event: 'share_failed', client, ext, error: String(e.message || e) });
      return result;
    }
  }

  const note = isImage
    ? `This chat may not display images returned by tools. To show the image to the user, give them this link: ${url}`
    : `Download link for the user: ${url}`;
  const content = Array.isArray(result.content) ? result.content : [];
  // ChatGPT turns results that contain image blocks into an empty object, which
  // would hide the link as well. Keep the image only for clients known to render it.
  const kept = keepsImages(metadata) ? content : content.filter((c) => c?.type !== 'image');
  return { ...result, content: [...kept, { type: 'text', text: note }] };
}

async function install() {
  const entry = process.argv[1];
  if (!entry || !process.argv.slice(2).includes('remote')) return;
  // Same canonical path the ESM loader uses for the main entry, so both imports
  // share one module instance and the patch below is the one the agent calls.
  let entryReal = entry;
  try { entryReal = fs.realpathSync(entry); } catch { /* keep as given */ }
  const target = path.join(path.dirname(entryReal), 'remote-device', 'desktop-commander-integration.js');
  let mod;
  try {
    mod = await import(pathToFileURL(target).href);
  } catch (e) {
    log({ event: 'hook_not_installed', reason: `cannot load ${target}: ${e.message}` });
    return;
  }
  const Cls = mod.DesktopCommanderIntegration;
  const original = Cls?.prototype?.callClientTool;
  if (typeof original !== 'function') {
    log({ event: 'hook_not_installed', reason: 'DesktopCommanderIntegration.callClientTool not found' });
    return;
  }
  Cls.prototype.callClientTool = async function patched(toolName, args, metadata) {
    const result = await original.call(this, toolName, args, metadata);
    try {
      return await augmentResult(toolName, args, metadata, result);
    } catch (e) {
      log({ event: 'hook_error', error: String(e.message || e) });
      return result;
    }
  };
  log({ event: 'hook_installed', entry });
}

await install();
