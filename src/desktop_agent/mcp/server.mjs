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
      timeout: 135000,
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

const server = new McpServer({
  name: 'desktop-agent',
  version: '1.0.0',
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
    description: 'List the generic background-first capabilities currently exposed by the local desktop agent.',
    inputSchema: z.object({}),
  },
  async () => {
    try {
      return toolResult(await request('POST', '/action', { action: 'capabilities' }, true));
    } catch (error) {
      return toolResult(error.response || { ok: false, error: String(error.message || error) }, true);
    }
  }
);

server.registerTool(
  'desktop_action',
  {
    title: 'Desktop Agent Action',
    description: 'Execute one structured local desktop-agent request. Prefer native APIs, adapters, COM, Playwright, UIA and Win32 before physical input.',
    inputSchema: z.object({
      request: z.record(z.string(), z.any()).describe('Complete desktop-agent request object, including its action field.'),
    }),
  },
  async ({ request: actionRequest }) => {
    try {
      return toolResult(await request('POST', '/action', actionRequest, true));
    } catch (error) {
      return toolResult(error.response || { ok: false, error: String(error.message || error) }, true);
    }
  }
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
