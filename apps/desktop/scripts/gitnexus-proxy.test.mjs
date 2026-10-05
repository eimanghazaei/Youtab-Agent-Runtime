import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { createGitNexusProxy } from '../../../optional-skills/research/gitnexus-explorer/scripts/proxy.mjs';

let root, server, port;
beforeAll(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-gitnexus-test-'));
  fs.mkdirSync(path.join(root, 'dist'));
  fs.writeFileSync(path.join(root, 'outside.txt'), 'PRIVATE_SYNTHETIC_OUTSIDE');
  fs.writeFileSync(path.join(root, 'dist', 'index.html'), '<html>synthetic app</html>');
  fs.writeFileSync(path.join(root, 'dist', 'asset.js'), 'synthetic asset');
  server = createGitNexusProxy(path.join(root, 'dist'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  port = server.address().port;
});
afterAll(async () => {
  await new Promise(resolve => server.close(resolve));
  // This path was created directly by mkdtemp, not supplied by the caller.
  fs.rmSync(root, { recursive: true, force: true });
});
function request(target) {
  return new Promise((resolve, reject) => {
    http.get({ hostname: '127.0.0.1', port, path: target }, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body, cache: response.headers['cache-control'] }));
    }).on('error', reject);
  });
}
describe('GitNexus static containment', () => {
  it.each(['/../outside.txt', '/%2e%2e/outside.txt', '/%2e%2e%2foutside.txt', '/..%5coutside.txt', '/%00outside.txt'])('rejects %s before reading outside assets', async target => {
    const result = await request(target);
    expect(result.status).toBe(403);
    expect(result.body).not.toContain('PRIVATE_SYNTHETIC_OUTSIDE');
  });
  it('rejects malformed URL encoding', async () => {
    expect((await request('/%E0%A4%A')).status).toBe(400);
  });
  it('preserves assets, SPA routes and cache policy', async () => {
    expect(await request('/asset.js')).toEqual({ status: 200, body: 'synthetic asset', cache: 'public, max-age=86400' });
    expect(await request('/workspace/route')).toEqual({ status: 200, body: '<html>synthetic app</html>', cache: 'no-cache' });
    expect((await request('/missing.js')).status).toBe(404);
  });
  it.skipIf(process.platform === 'win32')('rejects an outside symlink and a symlinked SPA fallback', async () => {
    fs.symlinkSync(path.join(root, 'outside.txt'), path.join(root, 'dist', 'linked.txt'));
    expect((await request('/linked.txt')).status).toBe(403);
    fs.unlinkSync(path.join(root, 'dist', 'index.html'));
    fs.symlinkSync(path.join(root, 'outside.txt'), path.join(root, 'dist', 'index.html'));
    expect((await request('/workspace/route')).status).toBe(403);
  });
});
