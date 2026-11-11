import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../src/${rel}`, import.meta.url)), 'utf8');
const feature = () => read('components/FeatureSettings.jsx');
const usage = () => read('components/UsageGuide.jsx');
const css = () => read('styles.css');

// 从源码里抽出 features 对象声明的键
function declaredKeys(src) {
  const block = src.slice(src.indexOf('const features = {'), src.indexOf('const fallbackTimeZones'));
  return [...block.matchAll(/^ {2}([a-z_]+):\[/gm)].map((m) => m[1]);
}

function placementKeys(src) {
  const common = src.slice(src.indexOf('const commonKeys'), src.indexOf('const featureGroups'));
  const groups = src.slice(src.indexOf('const featureGroups'), src.indexOf('export function FeatureSettings'));
  const arrays = [...groups.matchAll(/\[('(?:[a-z_]+'(?:,'[a-z_]+')*)?)\]/g)].map((m) => m[1]);
  return {
    common: [...common.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]),
    grouped: arrays.flatMap((a) => [...a.matchAll(/'([a-z_]+)'/g)].map((m) => m[1])),
  };
}

test('每个功能开关都归到了常用区或折叠组，一个都不能漏', () => {
  const src = feature();
  const all = declaredKeys(src);
  const { common, grouped } = placementKeys(src);
  assert.ok(all.length >= 20, `开关数量异常，只有 ${all.length} 个`);
  const missing = all.filter((k) => !common.includes(k) && !grouped.includes(k));
  assert.deepEqual(missing, [], `这些开关没归位，界面上会看不到：${missing.join(', ')}`);
  const dup = [...new Set([...common, ...grouped])].filter((k) => [...common, ...grouped].filter((x) => x === k).length > 1);
  assert.deepEqual(dup, [], `这些开关被放了两次：${dup.join(', ')}`);
  const ghost = [...common, ...grouped].filter((k) => !all.includes(k));
  assert.deepEqual(ghost, [], `这些键没有对应的开关声明：${ghost.join(', ')}`);
});

test('功能页监听设置保存事件，否则别的页保存后这里会撞版本冲突', () => {
  const src = feature();
  assert.match(src, /addEventListener\('lattice:settings-saved'/, '必须监听设置保存事件');
  assert.match(src, /event\.detail\.settings_version/, '要用返回的新版本号更新本地版本');
  assert.match(src, /removeEventListener\('lattice:settings-saved'/, '卸载时要解绑');
});

test('传给功能页的两个回调都真的被用到，不留悬空属性', () => {
  const src = feature();
  assert.match(src, /export function FeatureSettings\(\{onOpenSummary,onOpenEventGuide\}\)/);
  const usesSummary = (src.match(/onOpenSummary/g) || []).length;
  const usesGuide = (src.match(/onOpenEventGuide/g) || []).length;
  assert.ok(usesSummary >= 2, 'onOpenSummary 必须被绑定到按钮上');
  assert.ok(usesGuide >= 2, 'onOpenEventGuide 必须被绑定到按钮上');
});

test('常用区与折叠组的折叠控件有对应样式', () => {
  const sheet = css();
  assert.match(sheet, /\.settings-menu \{/, '.settings-menu 是折叠组的外壳，缺了会没有边框和缩进');
  assert.ok((sheet.match(/\.settings-menu/g) || []).length >= 5, 'settings-menu 相关规则过少');
});

test('折叠组没有把保存按钮或状态提示卷进去', () => {
  const src = feature();
  const groups = src.slice(src.indexOf('{featureGroups.map'), src.indexOf('<div className="settings-actions">'));
  assert.doesNotMatch(groups, /type="submit"/, '保存按钮不能在折叠组里');
  assert.doesNotMatch(groups, /role="status"/, '状态提示不能在折叠组里');
});

test('使用说明指向自动摘要管理页签，不再指向已迁走的配置页位置', () => {
  const src = usage();
  assert.doesNotMatch(src, /配置 → 自动摘要配置/, '自动摘要已迁到独立页签，旧路径会让人找不到入口');
  const summaryLinks = [...src.matchAll(/<SettingsLink tab="summary"[^>]*>([^<]*)<\/SettingsLink>/g)].map((m) => m[1]);
  assert.ok(summaryLinks.length >= 2, `应有至少两处指向自动摘要管理，实际 ${summaryLinks.length} 处`);
  for (const label of summaryLinks) {
    assert.equal(label, '自动摘要管理', `链接文字要与页签名一致，实际是「${label}」`);
  }
});

test('配置页签的 key 与使用说明里的链接一致', () => {
  const panel = read('components/SettingsPanel.jsx');
  const tabs = panel.slice(panel.indexOf('const tabs'), panel.indexOf('];', panel.indexOf('const tabs')));
  const keys = [...tabs.matchAll(/\["([a-z]+)"/g)].map((m) => m[1]);
  for (const key of ['summary', 'configuration', 'features', 'imports']) {
    assert.ok(keys.includes(key), `页签里缺 ${key}`);
  }
  const usageKeys = [...usage().matchAll(/<SettingsLink tab="([a-z]+)"/g)].map((m) => m[1]);
  const bad = [...new Set(usageKeys)].filter((k) => !keys.includes(k));
  assert.deepEqual(bad, [], `使用说明里引用了不存在的页签：${bad.join(', ')}`);
});
