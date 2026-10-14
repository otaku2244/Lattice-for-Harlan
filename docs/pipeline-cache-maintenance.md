# 流水线缓存维护：运行契约变更之后

## 背景

Serein 把一批源文件定为**运行契约**（清单在流水线模块的 `runtime_revision()`）。只要其中任一文件的内容变化，`runtime_revision()` 就不再相同，此前冻结的批次与归线路由生产者随即失效。

引擎只在本批重建时清理自己的缓存，不做全局回收。于是会留下三类残留：

| 残留 | 后果 |
|---|---|
| 陈旧 `route:` 批次（携带旧 revision） | 新批次与之重叠时复用其缓存，pre-Track 校验失败 → `needs_repair` |
| 失去归属证据的路由缓存（无主缓存） | 复用该行却无法证明来源，回落到已推进的 Track 卡 → `cached Track is newer than this frozen batch` → `needs_repair` |
| 未完成批次的 jobs 与 `work:pipeline` 租约 | 队列守卫判定 busy，界面按钮空转 |

表现出来的就是界面里"作废／重建"不断累积、同一聊天反复 `needs_repair`。

**核心约束**：`pipeline_routes`（缓存）与 `pipeline_route_provenance`（归属证据）必须成对清除。只清一边就会制造上表第二行的无主缓存。运维脚本据此设计，`tests/test_pipeline_operator_scripts.py` 也为此设了回归。

## 触发时机

- 修改过运行契约内的任一文件并重建容器之后；
- 或出现这些迹象时：`needs_repair` 反复出现、`superseded_repair` 持续累积、界面按钮点了没反应。

## 执行

三个脚本位于仓库 `scripts/` 下，默认只统计不写入，加 `--apply` 才真正执行。**容器内没有 `scripts/` 目录**（镜像只装运行所需代码），因此用 stdin 注入，交给容器的 Python 跑，并指向容器内的数据库 `/data/serein.db`：

```bash
INSTANCE=<memory 容器名>
DB=/data/serein.db
SCRIPT_DIR=<脚本目录>          # 仓库里是 scripts/；实例内用脚本实际所在目录
INSTANCE_ROOT=<实例安装目录>   # 宿主上数据库在 $INSTANCE_ROOT/deploy/runtime/serein.db

# 0) 先备份
cp $INSTANCE_ROOT/deploy/runtime/serein.db \
   $INSTANCE_ROOT/backups/serein-db-$(date -u +%Y%m%dT%H%M%SZ).db
```

```bash
# 1) 丢弃未完成的流水线工作，让未结算原文重新归线
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_reset.py
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_reset.py --apply

# 2) 清未结算的无主路由缓存
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_orphan_cache.py
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_orphan_cache.py --apply

# 3) 把已经卡住的批次交给产品自己的重建通道解开
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_repair.py
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_repair.py --apply
```

顺序 1 → 2 → 3：先撤掉陈旧生产者及其带来的无主缓存，再解已卡批次。反过来做等于白做——重建会先连上那些坏缓存。

## 各自保住的边界

- **`pipeline_reset.py`**：只动未完成的东西。已发布 Event、已结算原文（`raw_processing` 任一 outcome，含 `archived_only`、`skipped`）及其路由收据全部保留——`load_tracks()` 要靠这些行判断 Track 的近期活动。
- **`pipeline_orphan_cache.py`**：只清"未结算且无归属"的缓存行；已结算原文的路由行虽无 provenance 也照留，它们不会再被查询，缺证据无害。
- **`pipeline_repair.py`**：走 `pipeline_recovery.rebuild`，即界面"作废本批计划并重新归线"与 MCP `pipeline_rebuild` 的同一条路。旧批次转 `superseded_repair`，原冻结输入、job、模型输出与已保存 Event 一概保留；新批次带 `ignore_route_cache`，不会再撞回同一份坏缓存。

## 与界面出口的分工

| 场景 | 入口 |
|---|---|
| 单条批次卡住，想尽量保住已记录的路由历史 | 界面"重新校验并继续" |
| 单条批次卡住，确认归线无法恢复 | 界面"作废本批计划并重新归线"、MCP `pipeline_rebuild(batch_id, confirm="REBUILD_PIPELINE_BATCH")`、或 `pipeline_repair.py --apply` |
| 契约变更后成片积压 | `pipeline_reset.py` + `pipeline_orphan_cache.py` |

`needs_repair` 不能靠"重试此批次"解决：重试原样重放冻结输入，必然同样失败。`retry_batch` 只对 `paused_failure` 有效。归线恢复本身的分支与校验范围见 [归线历史恢复与明确重建](route-recovery.md)。

## 验证

```bash
docker exec -i $INSTANCE python - $DB < $SCRIPT_DIR/pipeline_orphan_cache.py    # 只看
```

关心三个数：`orphan_unsettled` 应为 0；`open_batches` 应回落到 0；`unsettled` 会随新原话自然增加，属正常。另可对备份副本跑 `PRAGMA quick_check`。

## 一个尚未覆盖的观察项

`pipeline_route_provenance` 里存在**指向已不存在批次**的行（历史遗留，涉及原话均已结算）。三个脚本都不检查这个方向，目前也未见它引发失败；如需彻底对齐，可在 `pipeline_orphan_cache.py` 的 `survey()` 里补一项统计。
