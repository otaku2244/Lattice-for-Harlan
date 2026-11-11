import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { latticeBackendTimedOut, callLatticeBackend } from '../server/latticeBackend.mjs';

// 回归背景：callLatticeBackend 用 AbortSignal.timeout() 设超时，
// 而 AbortSignal.timeout() 中止时抛的是 TimeoutError，不是 AbortError
// （只有 AbortController.abort() 才给 AbortError）。
// 因此旧写法 error?.name === "AbortError" 永远为假，所有超时被误判成 502。

async function slowServer(delayMs) {
  const server = http.createServer((req, res) => {
    setTimeout(() => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{}'); }, delayMs);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return { server, url: `http://127.0.0.1:${server.address().port}` };
}

const env = (url) => ({ LATTICE_MEMORY_URL: url, LATTICE_MEMORY_TOKEN: 'test-token' });

test('AbortSignal.timeout 抛 TimeoutError，旧判定漏判', async (t) => {
  const { server, url } = await slowServer(2000);
  t.after(() => server.close());
  let caught;
  try {
    await fetch(url, { signal: AbortSignal.timeout(50) });
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, '应当抛错');
  assert.equal(caught.name, 'TimeoutError');
  // 旧写法会漏掉这一种，正是本次修复的原因
  assert.equal(caught?.name === 'AbortError', false, '旧判定不应命中');
  assert.equal(latticeBackendTimedOut(caught), true, '新判定必须命中');
});

test('AbortSignal.timeout 的 signal.reason 同样带 TimeoutError', async () => {
  const signal = AbortSignal.timeout(10);
  await once(signal, 'abort');
  assert.equal(signal.reason.name, 'TimeoutError');
  assert.equal(latticeBackendTimedOut(signal.reason), true);
});

test('AbortController.abort 抛 AbortError，两种判定都命中', () => {
  const controller = new AbortController();
  controller.abort();
  const { reason } = controller.signal;
  assert.equal(reason.name, 'AbortError');
  assert.equal(latticeBackendTimedOut(reason), true);
});

test('非超时错误不被判为超时', () => {
  assert.equal(latticeBackendTimedOut(new TypeError('fetch failed')), false);
  assert.equal(latticeBackendTimedOut(new Error('lattice_backend_not_configured')), false);
  assert.equal(latticeBackendTimedOut(undefined), false);
  assert.equal(latticeBackendTimedOut({ status: 502 }), false);
});

test('真实调用：后端响应慢于超时预算时判为超时', async (t) => {
  const { server, url } = await slowServer(2000);
  t.after(() => server.close());
  let caught;
  try {
    // 300ms 预算 < 2s 响应延迟，必然超时
    await callLatticeBackend('/slow', {}, { env: env(url), timeout: 300 });
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, '应当超时抛错');
  assert.equal(latticeBackendTimedOut(caught), true, `实际 name=${caught?.name}`);
});

test('真实调用：后端不可连接时不是超时', async (t) => {
  const { server, url } = await slowServer(10);
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  let caught;
  try {
    await callLatticeBackend('/down', {}, { env: env(`http://127.0.0.1:${port}`), timeout: 5000 });
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, '应当抛错');
  assert.equal(latticeBackendTimedOut(caught), false, `name=${caught?.name} 不应判为超时`);
});

test('真实调用：成功响应不受影响', async (t) => {
  const { server, url } = await slowServer(10);
  t.after(() => server.close());
  const result = await callLatticeBackend('/fast', {}, { env: env(url), timeout: 5000 });
  assert.equal(result.ok, true);
  assert.equal(result.status, 200);
});

test('vite.config.mjs 不再用裸 AbortError 判定', () => {
  const source = readFileSync(fileURLToPath(new URL('../vite.config.mjs', import.meta.url)), 'utf8');
  const legacy = source.match(/error\?\.name\s*===\s*["']AbortError["']/g) || [];
  assert.equal(legacy.length, 0, `仍有 ${legacy.length} 处裸 AbortError 判定`);
  const uses = source.match(/latticeBackendTimedOut\(error\)/g) || [];
  assert.ok(uses.length > 0, 'vite.config.mjs 应当使用统一判定');
  // 导入必须来自后端封装，避免有人手写一份本地副本
  assert.match(source, /import \{[^}]*latticeBackendTimedOut[^}]*\} from '\.\/server\/latticeBackend\.mjs'/);
});
