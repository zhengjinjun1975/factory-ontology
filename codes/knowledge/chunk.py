#!/usr/bin/env python3
"""knowledge/chunk.py — 文本切块

把长文档切成适合向量检索与 LLM 上下文的片段。
结构优先：若文本含章节标题（以 # 开头或“数字.”开头的行），按标题切块；
否则按固定大小 + 重叠切分。每块记录在原文中的起止字符位置，便于溯源。

用法:
    from knowledge.chunk import chunk_text
    chunks = chunk_text(raw_text, size=600, overlap=80)
    # [{"index":0, "text":"...", "char_start":0, "char_end":...}, ...]
"""
import re

_HEADING_RE = re.compile(r"^\s*(#{1,6}\s+.*|[\d一二三四五六七八九十]+[、.．]\s+\S+.*)$")


def _split_headings(text):
    """按章节标题把文本切成若干 (标题, 正文) 片段。找不到标题返回 []。"""
    lines = text.splitlines()
    sections = []          # [(start_line, end_line)]
    starts = []
    for i, ln in enumerate(lines):
        if _HEADING_RE.match(ln.strip()):
            starts.append(i)
    if len(starts) < 2:    # 少于 2 个标题视为无结构
        return []
    for k, s in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        sections.append("\n".join(lines[s:end]).strip())
    return [s for s in sections if s]


def chunk_text(text, size=600, overlap=80):
    """把文本切成块列表。结构优先（按标题），否则固定大小+重叠。

    参数:
        text: 待切分纯文本
        size: 目标块字符数（固定切分时使用）
        overlap: 相邻块重叠字符数（固定切分时使用）
    返回:
        list[dict]: [{"index", "text", "char_start", "char_end"}, ...]
        异常/空输入返回 []。
    """
    try:
        if not text or not text.strip():
            return []
        if size < 1:
            size = 600
        if overlap < 0:
            overlap = 0
        if overlap >= size:
            overlap = int(size * 0.8)

        # 1) 结构优先：按章节标题切
        sections = _split_headings(text)
        chunks = []
        offset = 0
        if sections:
            for sec in sections:
                pos = text.find(sec, offset)
                if pos < 0:
                    pos = 0
                chunks.append({"index": len(chunks), "text": sec,
                               "char_start": pos, "char_end": pos + len(sec)})
                offset = pos + len(sec)
            return chunks

        # 2) 无结构：固定大小 + 重叠
        step = max(1, size - overlap)
        n = len(text)
        for i in range(0, n, step):
            piece = text[i:i + size]
            if not piece:
                break
            chunks.append({"index": len(chunks), "text": piece,
                           "char_start": i, "char_end": i + len(piece)})
            if i + size >= n:
                break
        return chunks
    except Exception:
        return []
