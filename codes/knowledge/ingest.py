#!/usr/bin/env python3
"""knowledge/ingest.py — 文档解析（PDF/Word/TXT → 纯文本）

把甲方交付的文档（.pdf/.docx/.txt）解析为 {title, raw_text}。
PDF 优先 pymupdf(fitz)，缺失则试 pdfplumber；docx 用 python-docx；txt 直接读。
任一依赖缺失或解析失败都返回 None（不抛异常），由调用方决定是否降级。

用法:
    from knowledge.ingest import extract_text
    doc = extract_text("甲方技术协议.pdf")   # {title, raw_text} 或 None
"""
import os


def extract_text(path):
    """解析文档为 {title, raw_text}。缺库/解析失败返回 None（不抛异常）。

    title 取文件名（去扩展名）；raw_text 为解析出的纯文本。
    """
    try:
        if not path or not os.path.isfile(path):
            return None
        title = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            raw = _extract_pdf(path)
        elif ext in (".docx", ".doc"):
            raw = _extract_docx(path)
        elif ext == ".txt":
            raw = _extract_txt(path)
        else:
            raw = None
        if not raw or not raw.strip():
            return None
        return {"title": title, "raw_text": raw.strip()}
    except Exception:
        return None


def _extract_pdf(path):
    """PDF → 文本。优先 pymupdf(fitz)，其次 pdfplumber。"""
    try:
        import fitz  # pymupdf
        text = ""
        with fitz.open(path) as doc:
            for page in doc:
                text += page.get_text() + "\n"
        return text
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        return None
    except Exception:
        return None


def _extract_docx(path):
    """Word .docx → 文本（python-docx）。"""
    try:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts.append("\t".join(c.text for c in row.cells))
        return "\n".join(parts)
    except Exception:
        return None


def _extract_txt(path):
    """TXT → 文本。兼容 UTF-8 / GBK / 常见编码。"""
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    return None
