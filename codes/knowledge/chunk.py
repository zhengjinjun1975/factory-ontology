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


def _split_paragraphs(text):
    """按段落(空行分隔)切分。段落是天然语义边界，优于固定长度硬切。"""
    paras = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paras if p.strip()]


def _split_sentences(text, size):
    """超长段落按句子边界(。！？\n)切到 ≤size，避免切断语义。"""
    if len(text) <= size:
        return [text]
    # 优先按中文/英文句子结束符切
    parts = [p.strip() for p in re.split(r"(?<=[。！？.!?])\s*|\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in parts:
        # 单句本身超长(无标点长串) → 先按固定大小拆
        if len(p) > size:
            if cur:
                chunks.append(cur); cur = ""
            for i in range(0, len(p), size):
                chunks.append(p[i:i + size])
            continue
        if cur and len(cur) + len(p) + 1 > size:
            chunks.append(cur); cur = p
        else:
            cur = (cur + " " if cur else "") + p
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text, size=600, overlap=80):
    """把文本切成块列表。语义优先：按段落(\n\n)切，超长段落按句子边界切，兜底固定大小。

    参数:
        text: 待切分纯文本
        size: 目标块字符数（超长段落/固定切分时使用）
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

        # 1) 优先按段落(空行)切分 — 段落是天然语义边界
        paras = _split_paragraphs(text)
        chunks = []
        offset = 0
        for para in paras:
            pos = text.find(para, offset)
            if pos < 0:
                pos = 0
            # 段落超长 → 按句子边界切(避免切断语义)
            pieces = _split_sentences(para, size)
            for piece in pieces:
                if not piece:
                    continue
                chunks.append({"index": len(chunks), "text": piece,
                               "char_start": pos, "char_end": pos + len(piece)})
            offset = pos + len(para)
        if chunks:
            return chunks

        # 2) 无段落(单段超长) → 固定大小 + 重叠兜底
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
