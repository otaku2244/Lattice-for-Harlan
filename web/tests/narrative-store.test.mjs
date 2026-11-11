import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const source = (name) => readFileSync(fileURLToPath(new URL(`../src/storage/${name}`, import.meta.url)), 'utf8');

// 用可控的 fetch 替身，验证 store 的网络契约，不依赖真实后端。
function withFetch(handler, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return Promise.resolve(fn()).finally(() => { globalThis.fetch = original; });
}

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test('projectLiveRoll 已导出，且 title 为空时回落到占位名', async () => {
  const { projectLiveRoll } = await import('../src/storage/narrativeStore.js');
  assert.equal(typeof projectLiveRoll, 'function', 'projectLiveRoll 必须导出，页面要靠它就地更新单条');
  const roll = projectLiveRoll({ narrative_id: 'n1', title: '', body: '正文' }, 0);
  assert.equal(roll.title, '未命名叙事卷');
});

test('projectLiveRoll 不再沿用旧显示名（改名后不会粘住旧标题）', async () => {
  const { projectLiveRoll } = await import('../src/storage/narrativeStore.js');
  const fallback = { displayTitle: '旧名字', sources: [] };
  const roll = projectLiveRoll({ narrative_id: 'n1', title: '新名字', body: '正文' }, 0, fallback);
  assert.equal(roll.title, '新名字');
});

test('projectSources 按 linked_*_ids 枚举：fallback 里没有的关联材料也会出现', async () => {
  const { projectLiveRoll } = await import('../src/storage/narrativeStore.js');
  const roll = projectLiveRoll({
    narrative_id: 'n1',
    title: '书',
    body: '正文',
    linked_scene_ids: ['10'],
    linked_event_ids: ['e-7'],
    linked_diary_ids: [3],
    linked_darkroom_ids: [],
    linked_upload_ids: [],
  }, 0, { sources: [{ type: 'scene', id: '10', title: '旧场景' }] });
  const keys = roll.sources.map((s) => `${s.type}:${s.id}`).sort();
  assert.deepEqual(keys, ['diary:3', 'event:e-7', 'scene:10']);
  // fallback 里有的用它的标题，没有的用 id 兜底
  assert.equal(roll.sources.find((s) => s.id === '10').title, '旧场景');
  assert.equal(roll.sources.find((s) => s.id === 'e-7').title, 'e-7');
});

test('loadNarrativeRolls 把 signal 透传给 fetch，并只保留有效书目', async () => {
  const { loadNarrativeRolls } = await import('../src/storage/narrativeStore.js');
  const controller = new AbortController();
  let seenSignal = null;
  await withFetch((url, options) => {
    seenSignal = options?.signal;
    return Promise.resolve(jsonResponse({
      status: 'ok',
      items: [
        { narrative_id: 'a', title: '有正文', body: '内容', publication_status: 'reviewed' },
        { narrative_id: 'b', title: '空书', body: '', publication_status: 'collecting' },
        { narrative_id: 'c', title: '空且非收集', body: '', publication_status: 'reviewed' },
      ],
    }));
  }, () => loadNarrativeRolls({ signal: controller.signal }));
  assert.equal(seenSignal, controller.signal, 'signal 必须原样传给 fetch，切页时才能中止旧请求');
});

test('loadNarrativeRolls 过滤掉既没有正文、也不在收集中的书目', async () => {
  const { loadNarrativeRolls } = await import('../src/storage/narrativeStore.js');
  const rolls = await withFetch(() => Promise.resolve(jsonResponse({
    status: 'ok',
    items: [
      { narrative_id: 'a', title: '有正文', body: '内容', publication_status: 'reviewed' },
      { narrative_id: 'b', title: '空书', body: '', publication_status: 'collecting' },
      { narrative_id: 'c', title: '空且非收集', body: '', publication_status: 'reviewed' },
    ],
  })), () => loadNarrativeRolls({}));
  assert.deepEqual(rolls.map((r) => r.id), ['a', 'b']);
});

test('loadNarrativeRolls 失败时抛错，不再静默回退到本地样卷', async () => {
  const { loadNarrativeRolls } = await import('../src/storage/narrativeStore.js');
  await assert.rejects(
    () => withFetch(() => Promise.resolve(jsonResponse({ status: 'error', reason: 'storage_unavailable' }, 503)),
      () => loadNarrativeRolls({})),
    (error) => {
      assert.match(error.message, /读取书架失败/);
      assert.match(error.message, /记忆存储暂不可用/, '错误要翻成人话，而不是只给 HTTP 503');
      return true;
    },
  );
});

test('loadNarrativeRolls 对无效 items 抛错，不把书架清空', async () => {
  const { loadNarrativeRolls } = await import('../src/storage/narrativeStore.js');
  await assert.rejects(
    () => withFetch(() => Promise.resolve(jsonResponse({ status: 'ok', items: null })),
      () => loadNarrativeRolls({})),
    /没有返回有效的书目列表/,
  );
});

test('narrativeErrorMessage 覆盖本地真实存在的错误码', async () => {
  const { narrativeErrorMessage } = await import('../src/storage/narrativeErrors.js');
  const cases = [
    ['storage_unavailable', /记忆存储暂不可用/],
    ['narrative_revision_changed', /已有新修改/],
    ['preview_fingerprint_mismatch', /重新预览后保存/],
    ['preview_job_missing', /预览任务已过期/],
    ['bound_material_unavailable', /部分材料无法读取/],
    ['at_least_two_narrative_materials_required', /至少需要保留两条材料/],
    ['invalid_delete_request', /删除请求缺少确认/],
  ];
  for (const [code, pattern] of cases) {
    assert.match(narrativeErrorMessage({ reason: code }, 409, '保存正文'), pattern, `${code} 应有专属文案`);
  }
});

test('narrativeErrorMessage 按状态码兜底，并保留原始码用于排查', async () => {
  const { narrativeErrorMessage } = await import('../src/storage/narrativeErrors.js');
  assert.match(narrativeErrorMessage({}, 504, '保存正文'), /等待服务响应超时/);
  assert.match(narrativeErrorMessage({}, 413, '保存正文'), /提交内容过大/);
  assert.match(narrativeErrorMessage({}, 401, '保存正文'), /登录或服务凭据已失效/);
  assert.match(narrativeErrorMessage({ reason: 'some_unknown_code' }, 500, '保存正文'), /HTTP 500 · some_unknown_code/);
});

test('narrativeErrorMessage 把材料级失败逐条列出', async () => {
  const { narrativeErrorMessage } = await import('../src/storage/narrativeErrors.js');
  const text = narrativeErrorMessage({
    reason: 'bound_material_unavailable',
    errors: [{ source_type: 'event', source_id: 'e1', reason: 'not_found' }],
  }, 400, '保存正文');
  assert.match(text, /Event e1：已删除或不存在/);
});

test('narrativeRequest 对非 JSON 响应给出「尚未确认结果」而不是静默失败', async () => {
  const { narrativeRequest } = await import('../src/storage/narrativeErrors.js');
  await assert.rejects(
    () => withFetch(() => Promise.resolve({ ok: false, status: 502, json: async () => { throw new SyntaxError('bad'); } }),
      () => narrativeRequest('/x', {}, '保存正文')),
    (error) => {
      assert.match(error.message, /无法读取的响应/);
      assert.match(error.message, /尚未确认结果/);
      return true;
    },
  );
});

test('narrativeRequest 把中止错误原样抛出，不当成业务失败', async () => {
  const { narrativeRequest } = await import('../src/storage/narrativeErrors.js');
  const abort = Object.assign(new Error('aborted'), { name: 'AbortError' });
  await assert.rejects(
    () => withFetch(() => Promise.reject(abort), () => narrativeRequest('/x', {}, '读取书架')),
    (error) => {
      assert.equal(error.name, 'AbortError');
      return true;
    },
  );
});

test('narrativeRequest 认可 accepted 里的状态，拒绝其它状态', async () => {
  const { narrativeRequest } = await import('../src/storage/narrativeErrors.js');
  const ok = await withFetch(() => Promise.resolve(jsonResponse({ status: 'created' })),
    () => narrativeRequest('/x', {}, '保存正文', ['created', 'updated']));
  assert.equal(ok.payload.status, 'created');

  await assert.rejects(
    () => withFetch(() => Promise.resolve(jsonResponse({ status: 'conflict' })),
      () => narrativeRequest('/x', {}, '保存正文', ['created', 'updated'])),
    /保存正文失败/,
  );
});

test('源码守卫：store 里除文件上传外都走 narrativeRequest', () => {
  const store = source('narrativeStore.js');
  // 上传走 FormData 流，无法经 narrativeRequest，是全文件唯一允许的裸 fetch
  const bare = [...store.matchAll(/await fetch\(/g)];
  assert.equal(bare.length, 1, `只允许 uploadNarrativeMaterial 一处裸 fetch，实际 ${bare.length} 处`);
  const uploadAt = store.indexOf('export async function uploadNarrativeMaterial');
  assert.ok(uploadAt > 0 && store.indexOf('await fetch(') > uploadAt, '那唯一一处必须在上传函数里');
  assert.match(store, /export function projectLiveRoll\(item, index = 0, fallback\)/);
  assert.match(store, /export async function loadNarrativeRolls\(\{ signal \} = \{\}\)/);
});

test('源码守卫：页面不再从 store 取本地样卷做回退', () => {
  const page = readFileSync(fileURLToPath(new URL('../src/pages/NarrativePage.jsx', import.meta.url)), 'utf8');
  assert.doesNotMatch(page, /readFallbackNarrativeRolls/, '书架失败改为明确报错，不再静默展示样卷');
  assert.match(page, /libraryError/, '必须保留可重试的错误提示');
  assert.match(page, /previewWarnings/, '本地独有的自检提示不能丢');
  assert.match(page, /unreviewedKeys/, '本地独有的未自检告警不能丢');
});
