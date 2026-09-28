import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const HOME = process.env.USERPROFILE || os.homedir();
const TOKEN_DIR = process.env.RDC_HARNESS_TOKEN_DIR || path.join(HOME, '.chatgpt-desktop-agent');
const TOKEN_FILE = path.join(TOKEN_DIR, 'desktop.token');
const BASE_HOST = '127.0.0.1';
const BASE_PORT = Number(process.env.DESKTOP_AGENT_PORT || 17322);

function token() {
  return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
}

const DEFAULT_TIMEOUT_MS = 135000;
const MAX_TIMEOUT_MS = 3600 * 1000 + 15000;

// Long-running actions carry their own timeout; the proxy must wait at least
// that long, otherwise the model sees a timeout while the work continues.
// Desktop actions use seconds, browser payloads use milliseconds.
function timeoutFor(payload) {
  const isBrowser = payload?.action === 'browser';
  const raw = Number(isBrowser ? payload?.payload?.timeout : payload?.timeout);
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_TIMEOUT_MS;
  const ms = isBrowser ? raw : raw * 1000;
  return Math.min(Math.max(DEFAULT_TIMEOUT_MS, ms + 15000), MAX_TIMEOUT_MS);
}

function request(method, route, payload = null, authenticated = false) {
  return new Promise((resolve, reject) => {
    const body = payload == null ? null : Buffer.from(JSON.stringify(payload), 'utf8');
    const headers = {};
    if (body) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      headers['Content-Length'] = String(body.length);
    }
    if (authenticated) headers['X-Desktop-Agent-Token'] = token();

    const req = http.request({
      host: BASE_HOST,
      port: BASE_PORT,
      path: route,
      method,
      headers,
      timeout: timeoutFor(payload),
    }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let parsed;
        try { parsed = JSON.parse(raw || '{}'); }
        catch { parsed = { ok: false, error: raw || 'invalid JSON response' }; }
        if ((res.statusCode || 500) >= 400) {
          const err = new Error(parsed.error || `HTTP ${res.statusCode}`);
          err.response = parsed;
          reject(err);
          return;
        }
        resolve(parsed);
      });
    });
    req.on('timeout', () => req.destroy(new Error('desktop-agent request timeout')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

function toolResult(value, isError = false) {
  const text = JSON.stringify(value, null, 2);
  return {
    content: [{ type: 'text', text }],
    isError,
  };
}

// A `look` result carries base64 image data: send it as an MCP image block
// and keep only the metadata as text, so the model sees pixels, not base64.
function withImage(response) {
  const image = response?.result?.image;
  if (!image?.data) return toolResult(response);
  const { image: _omit, ...meta } = response.result;
  return {
    content: [
      { type: 'image', data: image.data, mimeType: image.mime_type || 'image/jpeg' },
      { type: 'text', text: JSON.stringify({ ok: true, ...meta }, null, 2) },
    ],
  };
}

async function callAction(payload) {
  try {
    return withImage(await request('POST', '/action', payload, true));
  } catch (error) {
    return toolResult(error.response || { ok: false, error: String(error.message || error) }, true);
  }
}

const server = new McpServer({
  name: 'desktop-agent',
  version: '1.2.0',
});

server.registerTool(
  'desktop_health',
  {
    title: 'Desktop Agent Health',
    description: 'Check whether the local background desktop-agent service is alive.',
    inputSchema: z.object({}),
  },
  async () => {
    try {
      return toolResult(await request('GET', '/health'));
    } catch (error) {
      return toolResult({ ok: false, error: String(error.message || error) }, true);
    }
  }
);

server.registerTool(
  'desktop_capabilities',
  {
    title: 'Desktop Agent Capabilities',
    description: 'Start here. Lists every action of the local Windows desktop agent grouped by area '
      + '(uia, com, process, browser, adapter, physical, ...) with its effect (read/write/exec/physical), '
      + 'all browser actions, installed app adapters and the recommended routing order.',
    inputSchema: z.object({}),
  },
  async () => callAction({ action: 'capabilities' })
);

server.registerTool(
  'desktop_describe',
  {
    title: 'Describe Desktop Agent Action',
    description: 'Exact parameters, required fields and an example for one desktop action, one browser '
      + 'action or one app adapter (including the adapter\'s own actions). Use before an unfamiliar call.',
    inputSchema: z.object({
      name: z.string().optional().describe('Desktop action name, e.g. uia_click, com_call.'),
      browser: z.string().optional().describe('Browser action name, e.g. click, snapshot.'),
      adapter: z.string().optional().describe('Adapter name from capabilities, e.g. autocad.'),
    }),
  },
  async (args) => callAction({ action: 'describe', ...args })
);

server.registerTool(
  'desktop_look',
  {
    title: 'Look At Screen',
    description: 'See a window, a screen region or a monitor as an image. Use it when UIA or the DOM '
      + 'do not explain what is on screen (custom/GPU UIs, dialogs, drawings, visual check after an action). '
      + 'Target one window when possible: it is captured in the background without focusing it. '
      + 'The text part maps image pixels back to screen coordinates.',
    inputSchema: z.object({
      window: z.record(z.string(), z.any()).optional()
        .describe('UIA window selector, e.g. {"title_re": "AutoCAD"}. Omit for the whole screen.'),
      hwnd: z.number().int().optional().describe('Window handle, alternative to window.'),
      region: z.object({
        left: z.number().int(), top: z.number().int(),
        width: z.number().int().positive(), height: z.number().int().positive(),
      }).optional().describe('Screen rectangle in pixels.'),
      monitor: z.number().int().min(0).optional().describe('Monitor index, 0 = all monitors (default).'),
      max_side: z.number().int().min(200).max(4096).optional()
        .describe('Longest image side in pixels, default 1568. Lower = cheaper, higher = more detail.'),
    }),
  },
  async (args) => callAction({ action: 'look', ...args })
);

server.registerTool(
  'desktop_action',
  {
    title: 'Desktop Agent Action',
    description: 'Execute one desktop-agent request: {"action": "<name>", ...params}. '
      + 'Get names from desktop_capabilities and parameters from desktop_describe. '
      + 'Routing: app adapter (adapter_call) > browser DOM / COM > UIA > Win32 > physical input '
      + '(needs allow_physical=true). Externally visible browser clicks (send/pay/delete) need '
      + 'commit=true and prior user confirmation. Errors return a hint and valid alternatives.',
    inputSchema: z.object({
      request: z.record(z.string(), z.any()).describe('Request object with an action field, e.g. {"action":"windows","title_re":"Excel"}.'),
    }),
  },
  async ({ request: actionRequest }) => callAction(actionRequest)
);


server.registerTool(
  'desktop_send_file',
  {
    title: 'Send Local File To Chat',
    description: 'Stage a local file and return it as an MCP embedded binary resource so supporting chat clients can present it as a downloadable attachment.',
    inputSchema: z.object({
      path: z.string().min(1).describe('Absolute local path of the file to hand back to the user.'),
      name: z.string().min(1).optional().describe('Optional download/display filename.'),
      mimeType: z.string().min(1).optional().describe('Optional MIME override. Usually auto-detected.'),
      maxBytes: z.number().int().positive().max(50 * 1024 * 1024).optional().describe('Maximum file size allowed for this handoff. Default 25 MiB.'),
    }),
  },
  async ({ path: localPath, name, mimeType, maxBytes }) => {
    try {
      const response = await request(
        'POST',
        '/action',
        {
          action: 'file_handoff',
          path: localPath,
          name,
          mime_type: mimeType,
          max_bytes: maxBytes,
        },
        true
      );
      const staged = response?.result;
      if (!staged?.path) throw new Error('desktop-agent did not return a staged file path');

      const bytes = fs.readFileSync(staged.path);
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              ok: true,
              name: staged.name,
              size: staged.size,
              mime_type: staged.mime_type,
              sha256: staged.sha256,
              uri: staged.uri,
              note: 'The second content block is the actual file. Rendering/download behavior depends on the MCP client.'
            }, null, 2),
          },
          {
            type: 'resource',
            resource: {
              uri: staged.uri,
              mimeType: staged.mime_type || 'application/octet-stream',
              blob: bytes.toString('base64'),
              _meta: {
                filename: staged.name,
                size: staged.size,
                sha256: staged.sha256,
              },
            },
          },
        ],
      };
    } catch (error) {
      return toolResult(
        error.response || { ok: false, error: String(error.message || error) },
        true
      );
    }
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
