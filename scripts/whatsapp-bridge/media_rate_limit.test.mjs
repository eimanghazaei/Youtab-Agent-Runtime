import { strict as assert } from 'node:assert';
import { once } from 'node:events';
import test from 'node:test';
import express from 'express';

import { mediaSendLimiter } from './media_rate_limit.js';

test('media send limiter rejects the 61st request before the handler', async () => {
  const app = express();
  let handled = 0;
  app.post('/send-media', mediaSendLimiter, (_req, res) => {
    handled += 1;
    res.json({ success: true });
  });
  const server = app.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const { port } = server.address();
    const url = `http://127.0.0.1:${port}/send-media`;
    for (let i = 0; i < 60; i += 1) {
      const response = await fetch(url, { method: 'POST' });
      assert.equal(response.status, 200);
    }
    const limited = await fetch(url, { method: 'POST' });
    assert.equal(limited.status, 429);
    assert.equal((await limited.json()).error, 'Too many media sends; retry shortly');
    assert.equal(handled, 60);
    assert.ok(limited.headers.get('ratelimit'));
  } finally {
    await new Promise((resolve, reject) => server.close(err => err ? reject(err) : resolve()));
  }
});
