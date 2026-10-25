"""Optional minimum of two complete owned exchanges before Event writing."""

from copy import deepcopy
import re
import time

from .pipeline_materials import substantive_ids


# Clients can archive tool traffic as plain text under a user/assistant role.
# Such rows are protocol, never participant speech: a tool call or a tool result.
_LEAD_TOOL_CALL = re.compile(r'^\s*<tool_call[>\s]', re.I)
_LEAD_TOOL_RESULT = re.compile(r'^\s*<tool_result[_>\s]', re.I)


def _text_tool_protocol(message):
    """True when a row is only plain-text tool traffic, not an utterance."""
    text = message.get('content')
    if not isinstance(text, str):
        return False
    return bool(_LEAD_TOOL_CALL.match(text) or _LEAD_TOOL_RESULT.match(text))


def add_lookahead(components):
    """Show a few later originals from the same session without making them owned."""
    known = {int(message['id']): message for component in components
             for message in component.get('context_messages') or []}
    for component in components:
        tail = max(int(message['id']) for message in component['messages'])
        sessions = {message['session_id'] for message in component['messages']}
        extra, size = [], 0
        for source_id, message in sorted(known.items()):
            if source_id <= tail or message['session_id'] not in sessions:
                continue
            next_size = size + len(str(message.get('content') or ''))
            if len(extra) >= 6 or next_size > 12_000:
                break
            extra.append(message)
            size = next_size
        context = {int(message['id']): message for message in
                   [*component.get('context_messages', []), *extra]}
        component['context_messages'] = sorted(context.values(), key=lambda message: int(message['id']))
        component['boundary_lookahead'] = extra


def _visible(message):
    metadata = message.get('metadata') or {}
    return (message.get('role') in {'user', 'assistant'}
            and message.get('source') not in {'memory_event', 'trace', 'error'}
            and not _text_tool_protocol(message)
            and not any(metadata.get(key) for key in
                        ('memory_review_only', 'memory_event_source', 'draft', 'discarded')))


def exchanges(messages):
    """Group adjacent role runs; separate a completed exchange at the next user."""
    result, current = [], []
    for message in sorted(messages, key=lambda row: int(row['id'])):
        if not _visible(message):
            continue
        if current and message.get('session_id') != current[0].get('session_id'):
            result.append(current)
            current = []
        if {row['role'] for row in current} == {'user', 'assistant'} and message['role'] == 'user':
            result.append(current)
            current = []
        current.append(message)
    if current:
        result.append(current)
    return result


def count_rounds(owned_ids, messages):
    owned = set(owned_ids)
    count = 0
    for unit in exchanges(messages):
        if {row['role'] for row in unit} != {'user', 'assistant'} or not all(int(row['id']) in owned for row in unit):
            continue
        users = {int(row['id']) for row in unit if row['role'] == 'user'}
        if unit[0]['role'] == 'user' and any(
            type(target := (row.get('metadata') or {}).get('reply_to_user_message_id')) is int
            and target not in users for row in unit if row['role'] == 'assistant'
        ):
            continue
        count += 1
    return count


def validate(review, output, component):
    if not component.get('writer_round_gate'):
        return {}
    if not isinstance(review, dict) or not isinstance(review.get('events'), list):
        raise ValueError('Round admission needs per-Event decisions')
    messages = {int(row['id']): row for row in component.get('context_messages') or []}
    result = {}
    for row in review['events']:
        index = row['event_index']
        admission = row.get('admission')
        if not isinstance(admission, dict) or set(admission) != {'closed_by'}:
            raise ValueError('Every Event needs admission.closed_by')
        event = output['events'][index]
        owned = {item['source_message_id'] for item in event['source_bindings']}
        closure = admission['closed_by']
        if closure is not None:
            if not isinstance(closure, dict) or set(closure) != {'source_message_id', 'quote'}:
                raise ValueError('Admission closure needs a source ID and verbatim quote')
            source_id, quote = closure['source_message_id'], closure['quote']
            if (type(source_id) is not int or source_id not in messages or source_id < max(owned)
                    or not isinstance(quote, str) or not quote.strip()
                    or quote not in str(messages[source_id].get('content') or '')
                    or messages[source_id].get('session_id') not in
                    {messages[key].get('session_id') for key in owned if key in messages}):
                raise ValueError('Admission closure must quote later original in the same session')
        result[event['event_ref']] = admission
    return result


GATE_DEFERRAL_LIMIT = 3
# 一条 Event 至少需要几轮完整问答。上游默认 2 轮，但多数原话本身就是一轮问答，
# 稳定单元冻结后不会再合并，2 轮门槛等于永久 defer。这里降为 1。
MIN_ROUNDS = 1


def ensure_gate_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_gate_deferrals(
        raw_id INTEGER PRIMARY KEY,deferrals INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)""")


def exhausted_ids(conn, raw_ids, limit=GATE_DEFERRAL_LIMIT):
    """已经因轮次不足被 defer 达到上限的原话。"""
    keys = [int(key) for key in raw_ids]
    if not keys:
        return set()
    try:
        ensure_gate_table(conn)
        marks = ",".join("?" for _ in keys)
        rows = conn.execute(f"SELECT raw_id FROM pipeline_gate_deferrals WHERE deferrals>=? AND raw_id IN ({marks})",
                            [limit, *keys]).fetchall()
    except Exception:
        return set()
    return {int(row[0]) for row in rows}


def record_gate_outcome(conn, deferred_ids, resolved_ids):
    """defer 累积计数；已结算/已跳过的清零。"""
    ensure_gate_table(conn)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for raw_id in {int(key) for key in deferred_ids}:
        conn.execute("""INSERT INTO pipeline_gate_deferrals(raw_id,deferrals,updated_at) VALUES(?,1,?)
                        ON CONFLICT(raw_id) DO UPDATE SET deferrals=deferrals+1,updated_at=excluded.updated_at""",
                     (raw_id, now))
    for raw_id in {int(key) for key in resolved_ids}:
        conn.execute("DELETE FROM pipeline_gate_deferrals WHERE raw_id=?", (raw_id,))


def apply_gate(plan, component, exhausted=frozenset()):
    """Short open activities remain pending; tool traffic is consumed; exhausted units are released."""
    if not component.get('writer_round_gate'):
        return plan
    result = deepcopy(plan)
    scope = {int(row['id']): row for row in component.get('context_messages') or []}
    stable = {int(row['id']) for row in component['messages']}
    admissions = plan['event_admissions']
    accepted, pending, skipped, receipts = [], set(), set(), []
    for event in result['events']:
        admission = admissions[event['event_ref']]
        rounds = count_rounds(substantive_ids(event), scope.values())
        owned = set(event['source_message_ids'])
        waiting_reply = (admission['closed_by'] is None and any(
            {row['role'] for row in unit} == {'user'}
            and owned.intersection(int(row['id']) for row in unit)
            for unit in exchanges(scope.values())))
        visible = [row for row in scope.values()
                   if int(row['id']) in owned and _visible(row)]
        forced = bool(owned & set(exhausted))
        if not visible:
            # 纯工具流量（tool_call / tool_result）本质是协议，不是对话，凑不出轮次。
            # 继续 defer 会让整条队列永久堵在这一格，按协议语义直接消费掉。
            disposition = 'skip'
        elif forced:
            disposition = 'write'
        elif waiting_reply:
            disposition = 'defer'
        elif rounds >= MIN_ROUNDS:
            disposition = 'write'
        elif admission['closed_by']:
            disposition = 'skip'
        else:
            disposition = 'defer'
        receipts.append({'event_ref': event['event_ref'], 'rounds': rounds,
                         'minimum': MIN_ROUNDS, 'disposition': disposition, 'forced': forced})
        if disposition == 'write':
            accepted.append(event)
        else:
            (pending if disposition == 'defer' else skipped).update(owned & stable)
    while True:
        blocked = [event for event in accepted if pending.intersection(event['source_message_ids'])]
        if not blocked:
            break
        for event in blocked:
            pending.update(set(event['source_message_ids']) & stable)
            accepted.remove(event)
            next(row for row in receipts if row['event_ref'] == event['event_ref'])['disposition'] = 'defer_shared_bridge'
    retained = {source_id for event in accepted for source_id in event['source_message_ids']}
    result['events'] = accepted
    result['defer_source_message_ids'] = sorted(set(result['defer_source_message_ids']) | pending)
    result['skip_source_message_ids'] = sorted(set(result['skip_source_message_ids']) | (skipped - pending - retained))
    result['admission_receipts'] = receipts
    return result
