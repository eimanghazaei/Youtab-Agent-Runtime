import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as crypto from 'node:crypto';
import path from 'node:path';
import os from 'node:os';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const bridgePath = fileURLToPath(new URL('./bridge.js', import.meta.url));
const source = fs.readFileSync(bridgePath, 'utf8');
const capability = 'synthetic_capability_' + 'x'.repeat(22); // 43 characters, never a live credential.

async function loadBridge(root, environment = {}, { pairOnly = false } = {}) {
  const middleware = [];
  const routes = new Map();
  const calls = { parsed: 0, reads: [], sends: [], listens: 0, pairing: 0 };
  const app = {
    use(fn) { middleware.push(fn); },
    get(route, fn) { routes.set(`GET ${route}`, fn); },
    post(route, fn) { routes.set(`POST ${route}`, fn); },
    listen() { calls.listens++; }, // No sockets, real HTTP, or WhatsApp connections.
  };
  const express = () => app;
  express.json = () => (_req, _res, next) => { calls.parsed++; next(); };
  const context = vm.createContext({
    Buffer, setTimeout, clearTimeout,
    console: { log() {}, warn() {}, error() {} },
    process: {
      argv: ['node', bridgePath, '--session', path.join(root, 'session'), ...(pairOnly ? ['--pair-only'] : [])],
      env: { HOME: root, YOUTAB_AGENT_WHATSAPP_BRIDGE_CAPABILITY: capability, ...environment },
      platform: process.platform, getuid: process.getuid, uptime: () => 0,
    },
  });
  const helpers = {
    createBoundedMessageStore: () => ({ remember() {}, get() {} }),
    mediaPayloadForFile: ({ buffer, filePath }) => ({ document: buffer, fileName: path.basename(filePath) }),
    inferMediaType: () => 'document',
    buildTextSendPayload: (message) => ({ content: { text: message }, options: {} }),
  };
  const dependencies = {
    fs: {
      ...fs,
      existsSync: file => file === '/synthetic/arbitrary/authorized.pdf' || fs.existsSync(file),
      readFileSync(file, ...rest) {
        if (file === '/synthetic/arbitrary/authorized.pdf') {
          calls.reads.push(file);
          return Buffer.from('synthetic attachment');
        }
        return fs.readFileSync(file, ...rest);
      },
    },
    crypto,
    path: { default: path },
    os,
    url: { fileURLToPath },
    child_process: { execFileSync() { throw new Error('Unexpected subprocess'); } },
    express: { default: express },
    '@hapi/boom': { Boom: class {} },
    pino: { default: () => ({ child: () => ({}) }) },
    'qrcode-terminal': { default: {} },
    '@whiskeysockets/baileys': {
      useMultiFileAuthState: async () => { calls.pairing++; return { state: {}, saveCreds() {} }; },
      fetchLatestBaileysVersion: async () => ({ version: [1, 0, 0] }),
      makeWASocket: () => ({ ev: { on() {} } }),
    },
    './allowlist.js': { parseAllowedUsers: () => new Set(), matchesAllowedUser: () => false },
    './outbound_ids.js': { createOutboundIdTracker: () => ({ remember() {}, has: () => false }) },
    './owner_message_gate.js': {},
    './bridge_helpers.js': helpers,
  };
  const module = new vm.SourceTextModule(source + `
    export function connectSyntheticSocket() {
      sock = { sendMessage: async (chatId, payload) => {
        syntheticCalls.sends.push({ chatId, payload });
        return { key: { id: 'synthetic-message' } };
      }};
      connectionState = 'connected';
    }
    export function seedSyntheticMessage() { messageQueue.push({ body: 'synthetic incoming' }); }
  `, { context, identifier: new URL('./bridge.js', import.meta.url).href });
  context.syntheticCalls = calls;
  await module.link(async (specifier) => {
    const values = dependencies[specifier];
    assert.ok(values, `Unexpected module: ${specifier}`);
    // Unused Baileys/helper imports get inert functions; startSocket is never invoked.
    const imports = [...source.matchAll(new RegExp(`import \\{([^}]+)\\} from ['"]${specifier.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]`, 'g'))]
      .flatMap(match => match[1].split(',').map(name => name.trim()));
    const exports = { ...Object.fromEntries(imports.map(name => [name, () => {}])), ...values };
    const synthetic = new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
    return synthetic;
  });
  await module.evaluate();
  module.namespace.connectSyntheticSocket();
  async function request(method, route, authorization, body = {}) {
    const req = { headers: { host: '127.0.0.1:3000', authorization }, body, params: { id: 'synthetic-chat' } };
    const res = { statusCode: 200, status(code) { this.statusCode = code; return this; }, json(value) { this.body = value; return this; } };
    const chain = [...middleware, routes.get(`${method} ${route}`)];
    assert.ok(chain.at(-1), `Missing real route: ${route}`);
    async function invoke(index) {
      let next;
      await chain[index](req, res, () => { next = invoke(index + 1); return next; });
      if (next) await next;
    }
    await invoke(0);
    return res;
  }
  return { calls, request, module };
}

for (const authorization of [undefined, 'Bearer incorrect']) {
  test(`missing or wrong capability rejects actual read/effect handlers: ${authorization ? 'wrong' : 'missing'}`, async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-whatsapp-auth-test-'));
    try {
      const bridge = await loadBridge(root);
      bridge.module.namespace.seedSyntheticMessage();
      for (const [method, route] of [['POST', '/send-media'], ['POST', '/send'], ['POST', '/edit'], ['POST', '/send-poll'], ['POST', '/send-location'], ['POST', '/typing'], ['POST', '/read'], ['GET', '/messages'], ['GET', '/health'], ['GET', '/chat/:id']]) {
        const response = await bridge.request(method, route, authorization, { chatId: 'synthetic-chat', filePath: '/synthetic/arbitrary/authorized.pdf', message: 'synthetic message' });
        assert.equal(response.statusCode, 401);
      }
      assert.equal(bridge.calls.parsed, 0);
      assert.deepEqual(bridge.calls.reads, []);
      assert.deepEqual(bridge.calls.sends, []);
      const authorized = await bridge.request('GET', '/messages', `Bearer ${capability}`);
      assert.equal(authorized.body.length, 1); // Rejected callers did not drain the queue.
    } finally {
      assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
}

test('authorized real media handler preserves arbitrary legitimate attachment paths', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-whatsapp-auth-test-'));
  try {
    const bridge = await loadBridge(root);
    const response = await bridge.request('POST', '/send-media', `Bearer ${capability}`, {
      chatId: 'synthetic-chat', filePath: '/synthetic/arbitrary/authorized.pdf', mediaType: 'document',
    });
    assert.equal(response.statusCode, 200);
    assert.equal(response.body.success, true);
    assert.deepEqual(bridge.calls.reads, ['/synthetic/arbitrary/authorized.pdf']);
    assert.equal(bridge.calls.sends.length, 1);
    assert.equal(bridge.calls.sends[0].chatId, 'synthetic-chat');
    const stored = path.join(root, 'session', 'bridge-capability');
    assert.equal(fs.readFileSync(stored, 'utf8'), capability);
    if (process.platform !== 'win32') assert.equal(fs.statSync(stored).mode & 0o777, 0o600);
  } finally {
    assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('manual bridge starts create fresh random credentials without caller configuration', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-whatsapp-auth-test-'));
  try {
    await loadBridge(root, { YOUTAB_AGENT_WHATSAPP_BRIDGE_CAPABILITY: '' });
    const credential = path.join(root, 'session', 'bridge-capability');
    const first = fs.readFileSync(credential, 'utf8');
    assert.match(first, /^[A-Za-z0-9_-]{43}$/);
    await loadBridge(root, { YOUTAB_AGENT_WHATSAPP_BRIDGE_CAPABILITY: '' });
    assert.notEqual(fs.readFileSync(credential, 'utf8'), first);
  } finally {
    assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('capability publication refuses hard links without modifying the external target', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-whatsapp-auth-test-'));
  try {
    fs.mkdirSync(path.join(root, 'session'));
    const external = path.join(root, 'external-file');
    fs.writeFileSync(external, 'external sentinel', { mode: 0o600 });
    fs.linkSync(external, path.join(root, 'session', 'bridge-capability'));
    await assert.rejects(loadBridge(root), /private owned regular file/);
    assert.equal(fs.readFileSync(external, 'utf8'), 'external sentinel');
  } finally {
    assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('pair-only mode retains pairing without a bridge capability or HTTP server', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-whatsapp-auth-test-'));
  try {
    const bridge = await loadBridge(root, { YOUTAB_AGENT_WHATSAPP_BRIDGE_CAPABILITY: '' }, { pairOnly: true });
    await Promise.resolve();
    assert.equal(bridge.calls.pairing, 1);
    assert.equal(bridge.calls.listens, 0);
    assert.equal(fs.existsSync(path.join(root, 'session', 'bridge-capability')), false);
  } finally {
    assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true, force: true });
  }
});
