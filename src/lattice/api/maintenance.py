"""Local fork：清理历史版本（墓碑 / 被替换事件）及其文档与召回索引。

只删除 status 为 tombstoned / superseded 的事件，与 documents 中
lifecycle 为 deleted / superseded 的文档。活跃数据一律不碰。
"""
import glob
import os
import sqlite3

from fastapi import APIRouter
from fastapi.responses import JSONResponse

SKIP_TABLES = {"deletions"}                 # 保留删除事实，作为防复活凭证
SKIP_COLUMNS = {("personal_records", "key")}  # key 是收藏名，不是文档 id

# 子表在前，documents / fact_events 在后（外键 RESTRICT）
STEP_TEMPLATES = [
    ("fact_event_sources", "DELETE FROM fact_event_sources WHERE item_id IN (%s)", "event"),
    ("fact_event_replacement_edges",
     "DELETE FROM fact_event_replacement_edges WHERE predecessor_id IN (%s) OR successor_id IN (%s)", "event2"),
    ("event_replacements",
     "DELETE FROM event_replacements WHERE predecessor_id IN (%s) OR successor_id IN (%s)", "doc2"),
    ("evidence_actions",
     "DELETE FROM evidence_actions WHERE binding_id IN "
     "(SELECT id FROM evidence_bindings WHERE document_id IN (%s))", "doc"),
    ("evidence_bindings", "DELETE FROM evidence_bindings WHERE document_id IN (%s)", "doc"),
    ("narrative_materials",
     "DELETE FROM narrative_materials WHERE document_id IN (%s) OR target_id IN (%s)", "doc2"),
    ("revisions", "DELETE FROM revisions WHERE document_id IN (%s)", "doc"),
    ("personal_records", "DELETE FROM personal_records WHERE document_id IN (%s)", "doc"),
    ("import_tag_jobs", "DELETE FROM import_tag_jobs WHERE document_id IN (%s)", "doc"),
    ("tagging_outbox", "DELETE FROM tagging_outbox WHERE document_id IN (%s)", "doc"),
    ("pipeline_track_events", "DELETE FROM pipeline_track_events WHERE event_id IN (%s)", "event"),
    ("pipeline_event_details", "DELETE FROM pipeline_event_details WHERE event_id IN (%s)", "event"),
    ("pipeline_arc_links", "DELETE FROM pipeline_arc_links WHERE event_id IN (%s)", "event"),
    ("fact_event_arc_links", "DELETE FROM fact_event_arc_links WHERE event_id IN (%s)", "event"),
    ("fact_relation_proposals",
     "DELETE FROM fact_relation_proposals WHERE candidate_fact_id IN (%s) OR new_fact_id IN (%s)", "event2"),
    ("legacy_edge_imports",
     "DELETE FROM legacy_edge_imports WHERE source_id IN (%s) OR target_id IN (%s)", "doc2"),
    ("documents", "DELETE FROM documents WHERE id IN (%s)", "doc"),
    ("fact_events", "DELETE FROM fact_events WHERE item_id IN (%s)", "event"),
]


def _ph(seq):
    return ",".join("?" * len(seq))


def _open(database):
    conn = sqlite3.connect(database, timeout=180)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")
    return conn


def history_stats(database):
    with _open(database) as conn:
        dead_events = conn.execute(
            "SELECT count(*) FROM fact_events WHERE status IN ('tombstoned','superseded')").fetchone()[0]
        dead_docs = conn.execute(
            "SELECT count(*) FROM documents WHERE lifecycle IN ('deleted','superseded')").fetchone()[0]
        rows = conn.execute(
            "SELECT count(*) FROM fact_event_sources WHERE item_id IN "
            "(SELECT item_id FROM fact_events WHERE status IN ('tombstoned','superseded'))").fetchone()[0]
    return {
        "ok": True,
        "events": dead_events,
        "documents": dead_docs,
        "evidence_rows": rows,
        "size_mb": round(os.path.getsize(database) / 1e6, 1),
    }


def purge_history(database):
    before_size = os.path.getsize(database)
    conn = _open(database)
    try:
        dead_events = [r[0] for r in conn.execute(
            "SELECT item_id FROM fact_events WHERE status IN ('tombstoned','superseded')")]
        dead_docs = [r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE lifecycle IN ('deleted','superseded')")]
        if not dead_events and not dead_docs:
            return {"ok": True, "events": 0, "documents": 0, "rows": 0, "freed_mb": 0.0,
                    "message": "没有需要清理的历史版本。"}

        active_before = conn.execute(
            "SELECT count(*) FROM fact_events WHERE item_type='event' AND status='active'").fetchone()[0]
        total = 0
        with conn:
            conn.execute("UPDATE fact_events SET supersedes_item_id='' WHERE supersedes_item_id IN (%s)"
                         % _ph(dead_events), dead_events)
            for name, sql, kind in STEP_TEMPLATES:
                if kind == "event":
                    args = dead_events
                elif kind == "event2":
                    args = dead_events * 2
                elif kind == "doc":
                    args = dead_docs
                else:
                    args = dead_docs * 2
                total += max(conn.execute(sql, args).rowcount, 0)

        active_after = conn.execute(
            "SELECT count(*) FROM fact_events WHERE item_type='event' AND status='active'").fetchone()[0]
        if active_after != active_before:
            raise ValueError("活跃事件数量发生变化，已中止：%d → %d" % (active_before, active_after))

        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise ValueError("外键检查未通过（%d 处），已中止" % len(bad))

        conn.execute("VACUUM")
        index_rows = _purge_indexes(os.path.dirname(database), dead_docs)
        after_size = os.path.getsize(database)
        return {"ok": True, "events": len(dead_events), "documents": len(dead_docs),
                "rows": total, "index_rows": index_rows,
                "freed_mb": round((before_size - after_size) / 1e6, 1)}
    except sqlite3.OperationalError as error:
        return {"ok": False, "status": "busy", "message": "数据库正忙，请稍后重试：%s" % error}
    finally:
        conn.close()


def _purge_indexes(runtime_dir, dead_docs):
    if not dead_docs:
        return 0
    total = 0
    for path in glob.glob(os.path.join(runtime_dir, "model-indexes", "*", "index.sqlite")):
        index = sqlite3.connect(path, timeout=180)
        index.execute("PRAGMA secure_delete=ON")
        try:
            with index:
                for table, column in (("terms", "id"), ("vectors", "id"), ("documents", "id"),
                                      ("passages", "document_id"), ("passage_owners", "document_id"),
                                      ("entity_observations", "document_id")):
                    try:
                        total += max(index.execute(
                            "DELETE FROM %s WHERE %s IN (%s)" % (table, column, _ph(dead_docs)),
                            dead_docs).rowcount, 0)
                    except sqlite3.Error:
                        continue
            index.execute("VACUUM")
        finally:
            index.close()
    return total


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get('/v1/maintenance/history')
    def history():
        return history_stats(settings.database)

    @router.post('/v1/maintenance/history/purge')
    def purge(confirm: str = ''):
        if confirm not in ('1', 'true', 'yes'):
            return JSONResponse(status_code=400, content={
                'ok': False, 'message': '清理不可撤销，需带 confirm=1 调用。'})
        result = purge_history(settings.database)
        return JSONResponse(status_code=200 if result['ok'] else 409, content=result)

    return router
