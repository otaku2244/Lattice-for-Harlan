"""Local fork: 单条 Event 重写（仪表盘「重写」按钮用）。

拿库里已有的绑定原文，用 event_writer 的 create 口径（不给旧稿）重新拟标题、写正文；
机械校验不过就带 repair 提示重试，最多 EVENT_REWRITE_ATTEMPTS 次。

本模块只产出候选稿，落库由调用方走 events.revise —— 与手动修订共用同一条写入路径，
旧版照常转 superseded。

近似说明：track_cards / source_materials 不落库，无法还原，只能对齐 owned 绑定原文，
再补邻近非 owned 消息做 context、同 track 其它 active 事件做背景。与线上批次相比，
少了这两块策展输入，其余（prompt 构造器、角色规则、模型、校验函数）都是同一份代码。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import deployment
from ..model_runtime import complete
from . import pipeline_latest as L

EVENT_REWRITE_ATTEMPTS = 3
EVENT_REWRITE_TIMEOUT_SECONDS = 300
_CONTEXT_MESSAGES = 6
_CONTEXT_CHARS = 6000
_CONTEXT_WINDOW = 12


_LENGTH_VIOLATION_MARKERS = ('净字数', '不得长于', '长于 owned 原文', '超过 1000 字')


def _length_only(violations: list[str]) -> bool:
    """违规是否全部属于篇幅类。"""
    return bool(violations) and all(
        any(marker in item for marker in _LENGTH_VIOLATION_MARKERS) for item in violations)


def _length_budget_prompt(prompt: str, result: dict[str, Any],
                          owned_payload: list[dict[str, Any]]) -> str | None:
    """把「超过比例上限」翻译成一个具体字数，直接压给模型。

    比例类违规只说「超过 50%」，模型算不出该写多少字，实测连试 3 次仍会停在
    60%~90%。这里按 owned 原文净字数直接算出上限：原文达 RATIO_MIN（300 字）
    时取一半，不足时只要求不长于原文（与生产校验口径一致）。
    """
    source_chars = L._compact_len(L._writer_owned_text(owned_payload))
    if not source_chars:
        return None
    if source_chars >= L.EVENT_BODY_RATIO_MIN_SOURCE_CHARS:
        budget = int(source_chars * L.EVENT_BODY_SOURCE_RATIO_MAX)
    else:
        budget = source_chars
    budget = min(L.EVENT_BODY_ACCEPT_MAX_CHARS, max(60, budget))
    current = L._compact_len(str(result.get('event_draft') or ''))
    return prompt + (
        '\n篇幅硬预算：正文净字数硬性压到 %d 字以内（上一版 %d 字，owned 原文净字数 %d 字）。'
        '只留信息点本身，删掉全部解释、铺垫、复述、推导与评价；不得新增原文没有的内容；'
        '保留第一人称主体（“我”）；返回完整 JSON。' % (budget, current, source_chars))


def _rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def load_event_writer_inputs(database: str, item_id: str) -> dict[str, Any]:
    """还原 writer 需要的输入：owned 绑定原文、邻近 context、同 track 事件、activity roles。"""
    conn = sqlite3.connect('file:%s?mode=ro' % database, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        event = conn.execute(
            "select item_id, title, body, local_date, local_end_date, status, item_type"
            " from fact_events where item_id=?", (item_id,)).fetchone()
        if event is None:
            raise ValueError('item not found')
        if str(event['item_type']) != 'event':
            raise ValueError('only events can be rewritten')

        owned: list[dict[str, Any]] = []
        unmatched = 0
        synthetic = 900000
        for row in conn.execute(
                'select message_id, role, content, created_at from fact_event_sources'
                ' where item_id=? order by id', (item_id,)):
            hit = conn.execute('select id from raw_events where source_event_id=?',
                               (row['message_id'],)).fetchone()
            if hit:
                message_id = int(hit['id'])
            else:
                unmatched += 1
                synthetic += 1
                message_id = synthetic
            owned.append({'id': message_id, 'role': row['role'],
                          'content': row['content'], 'created_at': row['created_at']})
        owned.sort(key=lambda item: (str(item['created_at']), item['id']))

        roles = None
        track_id = ''
        detail = conn.execute('select details_json from pipeline_event_details where event_id=?',
                              (item_id,)).fetchone()
        if detail:
            parsed = json.loads(detail['details_json'])
            raw_roles = parsed.get('source_activity_roles') or {}
            roles = {int(key): value for key, value in raw_roles.items()} or None
            track_id = str(parsed.get('track_id') or '')

        owned_ids = {item['id'] for item in owned}
        context: list[dict[str, Any]] = []
        if owned_ids:
            neighbours = conn.execute(
                'select id, role, text, created_at from raw_events'
                ' where id between ? and ? and session_id in (select session_id from raw_events where id=?)'
                ' order by id desc',
                (min(owned_ids) - _CONTEXT_WINDOW, max(owned_ids) + _CONTEXT_WINDOW, max(owned_ids))).fetchall()
            chars = 0
            for row in neighbours:
                if int(row['id']) in owned_ids:
                    continue
                size = len(row['text'] or '')
                if len(context) < _CONTEXT_MESSAGES and chars + size <= _CONTEXT_CHARS:
                    context.append({'id': int(row['id']), 'role': row['role'],
                                    'content': row['text'], 'created_at': row['created_at']})
                    chars += size

        track_events: list[dict[str, Any]] = []
        if track_id:
            for row in conn.execute('select event_id from pipeline_track_events where track_id=?',
                                    (track_id,)):
                if row['event_id'] == item_id:
                    continue
                sibling = conn.execute(
                    "select item_id, title, body, local_date, local_end_date from fact_events"
                    " where item_id=? and status='active'", (row['event_id'],)).fetchone()
                if sibling and str(sibling['body'] or '').strip():
                    track_events.append({'event_id': sibling['item_id'], 'title': sibling['title'],
                                         'body': sibling['body'], 'local_date': sibling['local_date'],
                                         'local_end_date': sibling['local_end_date']})
    finally:
        conn.close()

    return {'event': {key: event[key] for key in event.keys()}, 'owned': owned,
            'context': context, 'track_events': track_events, 'roles': roles,
            'unmatched_sources': unmatched}


async def rewrite_event_draft(
    database: str,
    item_id: str,
    *,
    attempts: int = EVENT_REWRITE_ATTEMPTS,
    timeout: int = EVENT_REWRITE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """重写单条事件，返回候选稿（不落库）。ok=False 时 violations 说明卡在哪。"""
    inputs = load_event_writer_inputs(database, item_id)
    owned, context, roles = inputs['owned'], inputs['context'], inputs['roles']
    if not owned:
        raise ValueError('event has no bound sources to rewrite from')

    model = deployment.task_model(database, 'event_writer')
    if not model:
        raise ValueError('no event_writer model configured')

    prompt = L.build_event_writer_prompt(
        str(inputs['event'].get('local_date') or ''), '', owned,
        context_messages=owned + context,
        track_context_events=inputs['track_events'],
        track_cards=None,
        source_activity_roles=roles,
        previous_events=[],
        include_role_rules=False,
        append_only=False,
        context_read=True,
    )
    rules = L.materialize_agent_rules('event_writer')
    owned_payload = [item for item in L.event_reading_block_payload(owned, context, source_activity_roles=roles)
                     if item.get('evidence_role') == 'owned']
    call_model = {**model, 'request_timeout_seconds': timeout}

    result = None
    violations: list[str] = []
    used_prompt = prompt
    history: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        if attempt > 1 and result is not None:
            used_prompt = L.build_event_writer_repair_prompt(prompt, result, violations)
            if _length_only(violations):
                budgeted = _length_budget_prompt(prompt, result, owned_payload)
                if budgeted:
                    used_prompt = budgeted
        response = await complete(call_model, {
            'messages': [{'role': 'system', 'content': rules},
                         {'role': 'user', 'content': used_prompt}],
            'response_format': {'type': 'json_object'},
        })
        raw = response['choices'][0]['message'].get('content') or ''
        try:
            result = json.loads(raw)
        except Exception:
            result = None
            violations = ['模型没有返回合法 JSON']
            history.append({'attempt': attempt, 'error': 'invalid_json', 'raw_head': raw[:300]})
            continue
        violations = L.validate_event_writer_result(result, owned_payload)
        history.append({'attempt': attempt, 'violations': list(violations),
                        'evidence_sufficient': result.get('evidence_sufficient')})
        if str(result.get('event_draft') or '').strip() and not violations:
            break

    body = str((result or {}).get('event_draft') or '').strip()
    return {
        'ok': bool(result) and bool(body) and not violations,
        'title': str((result or {}).get('title') or '').strip(),
        'body': body,
        'evidence_sufficient': (result or {}).get('evidence_sufficient'),
        'violations': violations,
        'attempts': len(history),
        'history': history,
        'source_count': len(owned),
        'unmatched_sources': inputs['unmatched_sources'],
    }
