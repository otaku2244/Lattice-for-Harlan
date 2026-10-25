# -*- coding: utf-8 -*-
"""本地质量硬校验闸门（Local 资产，独立于官方文件）。

来源：原 ``pipeline_latest.validate_event_writer_result`` 第 27 行后追加的 32 行
（官方原码零改动，纯插入）。此处整块剥离到本地独有模块，官方文件仅保留 4 行钩子，
使官方更新时不必再三方对照。

剥离兼容说明：``_writer_owned_text`` 认 ``evidence_role`` + ``content``/``text``；
官方 ``pipeline_materials._SOURCE_FIELDS`` 用 ``role`` 而非 ``evidence_role``，
故额外认 ``role``，避免官方 canonical 结构下漏判。
"""
from __future__ import annotations

from typing import Any

# 篇幅双常量（本地语义）：单篇正文硬上限 / extend 时「旧正文＋新增段落」总量上限
EVENT_BODY_ACCEPT_MAX_CHARS = 1000
EVENT_BODY_TOTAL_MAX_CHARS = 1500

# 质量硬校验阈值。依据 scripts/overlap_probe.py 实测——合规改写 6-gram 重合约 12%~29%，
# 换人称的搬运稿约 59.7%，中间有 30 点以上空档。
EVENT_BODY_SOURCE_OVERLAP_MAX = 0.40
EVENT_DISCARDED_OVERLAP_MAX = 0.50
EVENT_BODY_OVERLAP_MIN_SOURCE_CHARS = 100
EVENT_BODY_RATIO_MIN_SOURCE_CHARS = 300
EVENT_BODY_OVERLAP_NGRAM = 6
EVENT_BODY_SOURCE_RATIO_MAX = 0.70

# extend 降级阈值（Local fork）：旧正文净字数达到此值，或旧正文本身是高重合的搬运稿，
# 就把本轮 extend 改成整篇 rewrite，给「旧正文纠错」留一个自动出口。
# 500 与 event_writer/AGENTS.md「一般不超过 500 字」对齐。
APPEND_DEMOTE_BODY_CHARS = 500


def _compact_len(text: str) -> int:
    """净字数（只计字母数字与汉字），供篇幅比例校验与重合检测用同一口径。"""
    return sum(1 for ch in str(text or '') if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')


def _overlap_ngrams(text: str, size: int) -> set[str]:
    compact = ''.join(ch for ch in str(text or '') if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')
    if len(compact) < size:
        return set()
    return {compact[i:i + size] for i in range(len(compact) - size + 1)}


def _overlap_ratio(subject: str, reference: str, size: int) -> float | None:
    """subject 的 size-gram 有多大比例在 reference 里逐字出现过。样本不足返回 None。"""
    subject_grams = _overlap_ngrams(subject, size)
    reference_grams = _overlap_ngrams(reference, size)
    if not subject_grams or not reference_grams:
        return None
    return len(subject_grams & reference_grams) / len(subject_grams)


def _writer_owned_text(owned_sources: list[dict[str, Any]] | None) -> str:
    """从 owned_sources 拼出 owned 原文。兼容两种形态：
    reading block payload（text / evidence_role）与 pipeline request 的原始 message（content）。"""
    parts: list[str] = []
    for item in owned_sources or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('evidence_role') or 'owned') != 'owned':
            continue
        for key in ('content', 'text'):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
                break
        for extra in item.get('evidence_texts') or []:
            if isinstance(extra, str) and extra.strip():
                parts.append(extra)
    return '\n'.join(parts)


def append_demote_reason(base: dict[str, Any], context_messages: list[dict[str, Any]] | None) -> str:
    """extend 之前判断是否该降级为整篇 rewrite。返回 '' 表示照常 append。

    两个信号：
      length  —— 旧正文净字数达到 APPEND_DEMOTE_BODY_CHARS，再追加只会更长；
      overlap —— 旧正文与它自己 owned 原文的 6-gram 重合超过搬运阈值，说明旧稿本身就是
                 换人称的搬运稿，append 只会把这个底子继续固化。
    只在 base 唯一时调用（extend 的 base 基数恰好为 1）。
    """
    body = str(base.get('body') or '')
    if not body.strip():
        return ''
    body_chars = _compact_len(body)
    if body_chars >= APPEND_DEMOTE_BODY_CHARS:
        return f'length:{body_chars}'
    source_ids = {int(value) for value in base.get('source_message_ids') or []}
    if not source_ids:
        return ''
    source_text = '\n'.join(str(item.get('content') or '') for item in context_messages or []
                            if int(item.get('id') or 0) in source_ids)
    if _compact_len(source_text) < EVENT_BODY_OVERLAP_MIN_SOURCE_CHARS:
        return ''
    ratio = _overlap_ratio(body, source_text, EVENT_BODY_OVERLAP_NGRAM)
    if ratio is not None and ratio > EVENT_BODY_SOURCE_OVERLAP_MAX:
        return f'overlap:{ratio:.2f}'
    return ''


def writer_body_violations(body: str, owned_sources: list[dict[str, Any]] | None = None,
                           discarded: list[str] | None = None) -> list[str]:
    """返回文体/篇幅类违规文本。

    官方 ``validate_event_writer_result`` 以
    ``violations.extend(event_quality_gate.writer_body_violations(body, owned_sources, discarded))``
    接入，插入位置必须是官方原第 27 行（``kept_details`` 检查）之后，以保持
    violations 列表顺序与剥离前逐字一致。
    """
    violations: list[str] = []
    discarded = list(discarded or [])
    # —— 质量硬校验（Local fork 追加）——
    if body:
        source_text = _writer_owned_text(owned_sources)
        source_chars = _compact_len(source_text)
        if source_chars >= EVENT_BODY_OVERLAP_MIN_SOURCE_CHARS:
            overlap = _overlap_ratio(body, source_text, EVENT_BODY_OVERLAP_NGRAM)
            if overlap is not None and overlap > EVENT_BODY_SOURCE_OVERLAP_MAX:
                violations.append(
                    f'正文与 owned 原文逐字重合 {overlap:.0%}，超过 {EVENT_BODY_SOURCE_OVERLAP_MAX:.0%} 上限；'
                    '这是把原文换了一遍人称的搬运稿，不是改写。请重组句式与语序，'
                    '把叮嘱／判断放回它发生的场景，并保住第一人称主体。')
        if source_chars:
            body_chars = _compact_len(body)
            if body_chars > source_chars:
                violations.append(
                    f'正文净字数 {body_chars} 字，超过 owned 原文净字数 {source_chars} 字；'
                    '原文只有几条时不能靠注水扩写凑成相当篇幅，请只保留原文真实承载的内容。')
            elif (source_chars >= EVENT_BODY_RATIO_MIN_SOURCE_CHARS
                  and body_chars > source_chars * EVENT_BODY_SOURCE_RATIO_MAX):
                violations.append(
                    f'正文净字数 {body_chars} 字，是 owned 原文 {source_chars} 字的 {body_chars / source_chars:.0%}，'
                    f'超过 {EVENT_BODY_SOURCE_RATIO_MAX:.0%} 上限；只有几条原文时更不能逐条展开，请按真实信息量取舍。')
        if '我' not in body:
            violations.append(
                '正文通篇没有第一人称主体（未出现“我”）；'
                '第一人称正文必须有视角锚点，不能写成无主语的旁白。')
        for detail in discarded:
            inflow = _overlap_ratio(body, detail, EVENT_BODY_OVERLAP_NGRAM)
            if inflow is not None and inflow > EVENT_DISCARDED_OVERLAP_MAX:
                violations.append(
                    f'discarded_details 里的内容又出现在正文里（重合 {inflow:.0%}）：{detail[:30]}；'
                    'discarded_details 只能列彻底删去的内容。')

    return violations
