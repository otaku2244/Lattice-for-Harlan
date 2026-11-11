import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { narrativePreviewFailure } from '../server/narrativeErrors.mjs';
import { writerFailureCode, runNarrativeCodexTask } from '../server/narrativeCodexRunner.mjs';

const roleDir = fileURLToPath(new URL('../codex_agents/narrative_writer/', import.meta.url));
const source = (name) => readFileSync(fileURLToPath(new URL(`../server/${name}`, import.meta.url)), 'utf8');

// 模型返回的七个格式细分码
const FORMAT_CODES = [
  'narrative_writer_result_not_object',
  'narrative_writer_result_types_invalid',
  'narrative_writer_result_schema_invalid',
  'narrative_writer_issues_invalid',
  'narrative_writer_review_invalid',
  'narrative_writer_review_schema_invalid',
  'narrative_writer_review_types_invalid',
];

test('格式细分码共用一个出口，但 reason 仍保留精确码', () => {
  const results = FORMAT_CODES.map((code) => narrativePreviewFailure(new Error(code)));
  const messages = new Set(results.map((r) => r.payload.message));
  assert.equal(messages.size, 1, '七个码应当只有一句文案');
  assert.match([...messages][0], /再写一次/);
  results.forEach((result, index) => {
    assert.equal(result.statusCode, 502);
    assert.equal(result.payload.reason, FORMAT_CODES[index], 'reason 不应被归组吞掉');
    assert.deepEqual(result.payload.writes_performed, []);
    assert.equal(result.payload.status, 'error');
  });
});

test('书写超时是 504，其余上游问题是 502', () => {
  const timeout = narrativePreviewFailure(new Error('narrative_writer_timeout'));
  assert.equal(timeout.statusCode, 504);
  assert.match(timeout.payload.message, /5 分钟/);

  for (const code of ['narrative_writer_auth_failed', 'narrative_writer_rate_limited',
    'narrative_writer_model_unavailable', 'narrative_writer_upstream_unavailable']) {
    const result = narrativePreviewFailure(new Error(code));
    assert.equal(result.statusCode, 502, `${code} 应为 502`);
    assert.equal(result.payload.reason, code);
  }
});

test('配置类错误各自给出可行动的说明', () => {
  const cases = [
    ['narrative_writer_disabled', /还没启用/],
    ['narrative_writer_disabled_main_model_authoring', /主模型.*网页 Writer/],
    ['narrative_writer_command_not_configured', /LATTICE_WRITER_COMMAND/],
    ['narrative_writer_model_not_configured', /选一个/],
    ['narrative_writer_auth_failed', /密钥/],
    ['narrative_writer_rate_limited', /限流/],
    ['narrative_writer_sufficient_result_invalid', /材料/],
    ['narrative_runner_failed', /2 MB/],
    ['narrative_identity_unavailable', /实例姓名/],
  ];
  for (const [code, pattern] of cases) {
    const { payload } = narrativePreviewFailure(new Error(code));
    assert.match(payload.message, pattern, `${code} 的文案不对：${payload.message}`);
    assert.equal(payload.reason, code);
  }
});

test('后端超时与未知异常都落在兜底，但原因码可区分', () => {
  const timeout = Object.assign(new Error('请求超时'), { name: 'TimeoutError' });
  const timedOut = narrativePreviewFailure(timeout);
  assert.equal(timedOut.statusCode, 504);
  assert.equal(timedOut.payload.reason, 'narrative_preview_timeout');

  const unknown = narrativePreviewFailure(new Error('some unexpected failure'));
  assert.equal(unknown.statusCode, 502);
  assert.equal(unknown.payload.reason, 'narrative_preview_failed');

  assert.equal(narrativePreviewFailure(undefined).statusCode, 502);
});

test('模型端状态码按成因分流', () => {
  const mapping = new Map([
    [504, 'narrative_writer_timeout'],
    [401, 'narrative_writer_auth_failed'],
    [403, 'narrative_writer_auth_failed'],
    [429, 'narrative_writer_rate_limited'],
    [400, 'narrative_writer_model_unavailable'],
    [404, 'narrative_writer_model_unavailable'],
    [422, 'narrative_writer_model_unavailable'],
    [500, 'narrative_writer_upstream_unavailable'],
    [503, 'narrative_writer_upstream_unavailable'],
  ]);
  for (const [status, code] of mapping) {
    assert.equal(writerFailureCode(status), code, `status ${status} 分流错误`);
  }
});

test('真实调用：模型返回 401 时报密钥失效，不再笼统说上游失败', async () => {
  const backend = async (path) => (path === '/v1/settings'
    ? { ok: true, payload: { identity: {}, upstream: { writer_enabled: true },
        models: [{ id: 'writer', model: 'synthetic' }], assignments: { writer: 'writer' } } }
    : { ok: false, status: 401, payload: {} });
  await assert.rejects(
    runNarrativeCodexTask({ mode: 'rewrite', title: 'T', currentBody: '', materials: {}, roleDir }, { backend }),
    /narrative_writer_auth_failed/);
});

test('源码守卫：runner 抛出的每个码都必须在映射表里有登记', () => {
  const runner = source('narrativeCodexRunner.mjs');
  // 取 runner 里所有 narrative_* 字面量。比只匹配 new Error("...") 更狠：
  // 三元表达式里的码（如 timedOut ? "a" : "b"）也能抓到。
  // narrative_preview 是任务名不是错误码，排除。
  const NON_ERROR_NAMES = new Set(['narrative_preview']);
  const mentioned = new Set([...runner.matchAll(/"(narrative_[a-z_]+)"/g)].map((m) => m[1])
    .filter((code) => !NON_ERROR_NAMES.has(code)));
  assert.ok(mentioned.size >= 15, `提取到的码太少，正则可能失效：${[...mentioned].join(', ')}`);
  // 已被 writerFailureCode 取代/从runner 移除，但仍在表里兼容历史回放
  const LEGACY_ONLY = new Set(['narrative_upstream_failed', 'narrative_writer_invalid_output']);
  const missing = [...mentioned].filter((code) => {
    if (LEGACY_ONLY.has(code)) return false;
    const { payload } = narrativePreviewFailure(new Error(code));
    return payload.reason !== code; // 落到兜底就说明没登记
  });
  assert.deepEqual(missing, [], `这些码没有专属文案，会落到兜底句：${missing.join(', ')}`);
});

test('源码守卫：预览的两个出口都走同一张表', () => {
  assert.match(source('narrativePreviewJobs.mjs'), /finish\(narrativePreviewFailure\(error\)\)/);
  const config = readFileSync(fileURLToPath(new URL('../vite.config.mjs', import.meta.url)), 'utf8');
  assert.match(config, /const failure = narrativePreviewFailure\(error\)/);
  // 不许再有三元表达式就地拼兜底文案的老写法
  assert.doesNotMatch(config, /暂时没有生成叙事卷预览/);
  assert.doesNotMatch(source('narrativePreviewJobs.mjs'), /暂时没有生成叙事卷预览/);
});

// 直接的作业出口验证：不经过 vite.config，本地缺 node_modules 也能跑
const loadJobs = async () => (await import('../server/narrativePreviewJobs.mjs')).createNarrativePreviewJobs;

test('预览作业失败时带上具体原因，不再只写 narrative_preview_failed', async (t) => {
  t.mock.method(console, 'error', () => {});
  const createNarrativePreviewJobs = await loadJobs();
  const jobs = createNarrativePreviewJobs({ now: () => 0 });

  const id = jobs.start(async () => { throw new Error('narrative_writer_model_not_configured'); });
  await new Promise((resolve) => setImmediate(resolve));
  const { result } = jobs.get(id);
  assert.equal(result.statusCode, 502);
  assert.equal(result.payload.reason, 'narrative_writer_model_not_configured');
  assert.match(result.payload.message, /选一个/);
  assert.deepEqual(result.payload.writes_performed, []);
});

test('预览作业遇到后端超时仍是 504', async (t) => {
  t.mock.method(console, 'error', () => {});
  const createNarrativePreviewJobs = await loadJobs();
  const jobs = createNarrativePreviewJobs({ now: () => 0 });

  const id = jobs.start(async () => {
    throw Object.assign(new Error('fetch failed'), { name: 'TimeoutError' });
  });
  await new Promise((resolve) => setImmediate(resolve));
  const { result } = jobs.get(id);
  assert.equal(result.statusCode, 504);
  assert.equal(result.payload.reason, 'narrative_preview_timeout');
});

test('预览作业成功时结果原样返回', async (t) => {
  t.mock.method(console, 'error', () => {});
  const createNarrativePreviewJobs = await loadJobs();
  const jobs = createNarrativePreviewJobs({ now: () => 0 });
  const id = jobs.start(async () => ({ statusCode: 200, payload: { status: 'ok', body: '正文' } }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(jobs.get(id).result.statusCode, 200);
  assert.equal(jobs.get(id).result.payload.body, '正文');
});
