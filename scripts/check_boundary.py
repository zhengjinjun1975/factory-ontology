#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_boundary.py — 开源边界自检 (纯标准库, 零第三方依赖)

三类检查
--------
A. 甲方痕迹: 库内(仅看 git 跟踪文件)不得出现
     A1 真实企业名/客户名 —— 命中 BANNED_COMPANIES 清单即违规
        (种子与示例应统一用「示例制造公司 / 示例企业B」这类占位名)
     A2 本机/内部绝对路径 —— 命中 INTERNAL_ROOTS(开发机目录、个人目录、
        本机解释器路径)即违规。开源副本不应暴露本机路径。
B. 开源边界:
     B1 不得 import 开源引擎(OPEN_SOURCE_ENGINES 黑名单), 尤其模块级 import
     B2 本库自查: 模块级 import 的模块必须能解析(标准库 / 本库 / 已声明依赖),
        解析不到的即「import 了不存在的模块」= 违规
C. 第三方依赖(核心路径):
     C1 模块级引用的第三方包, 必须已在 requirements*.txt 声明; 未声明即违规
     C2 核心引擎文件(问答/本体/检索/流程)必须纯标准库, 不得引入第三方

说明: 检查只看「模块级(顶层)」import; 函数/ try 内的惰性 import(可选依赖、
降级路径)单独列为 INFO, 不计违规 —— 这是仓库既有的可选依赖约定。

用法
----
  python scripts/check_boundary.py
  python scripts/check_boundary.py --json out.json
退出码: 0 = 通过; 1 = 存在违规。
"""
import os
import re
import sys
import ast
import json
import argparse
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── A1: 真实企业名关键词(完整名, 避免短名误伤) ──────────────────────────────
# 注: 本清单以 \u 转义 / 相邻字面量拼接书写, 使被禁词本身**不出现在本文件源码文本里**
#     —— 否则自检工具会命中自身(自命中)。运行期解码后与转义前完全一致。
BANNED_COMPANIES = [
    "\u534e\u80fd", "huan" "eng", "\u56fd\u5bb6\u7535\u7f51", "\u5357\u65b9\u7535\u7f51",
    "\u4e2d\u77f3\u6cb9", "\u4e2d\u77f3\u5316", "\u4e2d\u6d77\u6cb9",
    "\u4e2d\u56fd\u79fb\u52a8", "\u4e2d\u56fd\u8054\u901a", "\u4e2d\u56fd\u7535\u4fe1",
    "\u5bcc\u58eb\u5eb7", "\u5b81\u5fb7\u65f6\u4ee3", "\u4e09\u4e00\u91cd\u5de5",
    "\u6bd4\u4e9a\u8fea", "\u6f4d\u67f4\u52a8\u529b", "\u4e2d\u56fd\u4e2d\u8f66",
    "\u5b9d\u94a2", "\u978d\u94a2", "\u6d77\u5c14\u96c6\u56e2", "\u683c\u529b\u7535\u5668",
    "\u7f8e\u7684\u96c6\u56e2", "\u534e\u4e3a\u6280\u672f", "\u5b57\u8282\u8df3\u52a8",
    "\u963f\u91cc\u5df4\u5df4", "\u817e\u8baf", "\u4eac\u4e1c\u96c6\u56e2",
]
# ── A2: 本机/内部路径根(命中即违规) ────────────────────────────────────────
INTERNAL_ROOTS = [
    "Users", "Python312", "Python311", "Python310", "factory-ontology",
    "knowledge-base", "ontology-core", "opa-monitor", "sme-decision-ontology",
    "code-agent-lab", "obsidian-vault",
]
# 文档里的纯示例路径(白名单, 避免误报): C:/x、C:/x/factory.db 之类
BENIGN_PATH_RE = re.compile(r'^[A-Za-z]:[\\/]x\b', re.I)

# ── B1: 开源引擎黑名单(模式串, 命中模块名或其前缀) ──────────────────────────
OPEN_SOURCE_ENGINES = ["solo", "kag", "openspg", "vendor", "opa_monitor"]

# ── C2: 核心引擎文件(必须纯标准库) ─────────────────────────────────────────
CORE_ENGINE = [
    "ontology_qa_v3.py", "schema_ontology.py", "multi_table.py", "graph_rag.py",
    "csv_to_owl.py", "flow_engine.py", "kb_registry.py", "db_dialect.py",
    "industrial_dict_loader.py", "evidence.py", "ask_service.py", "audit_chain.py",
    "ontology_check.py", "ontology_health.py", "ontology_quality.py",
    "ontology_stats.py", "ontology_sparql.py", "logical_qa.py", "lexicon.py",
    "bm25_retrieval.py", "vector_retrieval.py", "fusion.py",
    "query_understand.py", "evidence_norm.py",
]

# pip 包名 -> 实际 import 名 的别名(requirements 解析用)
PKG_ALIAS = {
    "uvicorn": {"uvicorn"}, "fastapi": {"fastapi"}, "pydantic": {"pydantic"},
    "requests": {"requests"}, "python-multipart": {"multipart"},
    "httpx": {"httpx"}, "pytest": {"pytest"}, "pymupdf": {"fitz", "pymupdf"},
    "pdfplumber": {"pdfplumber"}, "python-docx": {"docx"},
    "openpyxl": {"openpyxl"}, "pymysql": {"pymysql"},
    "psycopg2-binary": {"psycopg2"}, "faster-whisper": {"faster_whisper"},
    "edge-tts": {"edge_tts"}, "sounddevice": {"sounddevice"}, "numpy": {"numpy"},
}


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def tracked_files():
    """git 跟踪文件(排除 .git); 非 git 环境回退到 os.walk。"""
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return [f for f in out.stdout.splitlines() if not f.startswith(".git/")]
    except Exception:
        pass
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "__pycache__")]
        for f in fn:
            files.append(os.path.relpath(os.path.join(dp, f), ROOT).replace("\\", "/"))
    return files


def scan_text_patterns(files, patterns, label):
    """在文本文件里找 patterns 命中; 返回 [(rel, lineno, keyword, linetext)]。"""
    hits = []
    rx = re.compile("|".join(re.escape(p) for p in patterns), re.I)
    for rel in files:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        txt = _read(p)
        if not txt:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            m = rx.search(line)
            if m:
                hits.append((rel, i, m.group(0), line.strip()[:120]))
    return hits


def scan_internal_paths(files):
    roots = "|".join(re.escape(r) for r in INTERNAL_ROOTS)
    rx = re.compile(r'(?<![A-Za-z0-9_/])(?:[A-Za-z]:[\\/](?:' + roots + r')|/(?:home|Users)/[A-Za-z0-9_.-]+)',
                    re.I)
    hits = []
    for rel in files:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        txt = _read(p)
        if not txt:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            for m in rx.finditer(line):
                seg = m.group(0)
                if BENIGN_PATH_RE.match(seg):
                    continue
                hits.append((rel, i, seg, line.strip()[:120]))
    return hits


def iter_py_files():
    for base in ("codes", "scripts"):
        b = os.path.join(ROOT, base)
        for dp, dn, fn in os.walk(b):
            dn[:] = [d for d in dn if d not in ("__pycache__", ".pytest_cache")]
            for f in fn:
                if f.endswith(".py"):
                    yield os.path.relpath(os.path.join(dp, f), ROOT).replace("\\", "/")


def local_module_names():
    names = set()
    for base in ("codes", "scripts"):
        b = os.path.join(ROOT, base)
        if not os.path.isdir(b):
            continue
        for f in os.listdir(b):
            if f.endswith(".py"):
                names.add(f[:-3])
        for d in os.listdir(b):
            if os.path.isdir(os.path.join(b, d)) and os.path.exists(os.path.join(b, d, "__init__.py")):
                names.add(d)
    for sub in ("agents", "tests", "plugins", "core", "knowledge", "flows", "config", "export"):
        p = os.path.join(ROOT, "codes", sub)
        if os.path.isdir(p):
            for f in os.listdir(p):
                if f.endswith(".py"):
                    names.add(f[:-3])
    return names


def declared_packages():
    """解析 requirements*.txt, 返回可 import 名集合。"""
    imports = {"multipart"}  # python-multipart 的 import 名
    for fn in ("requirements.txt", "requirements-dev.txt", "requirements-optional.txt"):
        txt = _read(os.path.join(ROOT, fn))
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            pkg = re.split(r'[<>=!\[; ]', line, 1)[0].strip().lower()
            if not pkg:
                continue
            if pkg in PKG_ALIAS:
                imports |= PKG_ALIAS[pkg]
            else:
                imports.add(pkg.replace("-", "_"))
    return imports


def parse_imports(rel):
    """返回 (top_level_imports, lazy_imports); 每项为 (top_module, lineno)。"""
    src = _read(os.path.join(ROOT, rel))
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [], []
    top, lazy = [], []

    def mods(node):
        out = []
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.append((node.module.split(".")[0], node.lineno))
        return out

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top.extend(mods(node))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for m in mods(node):
                if m not in top:
                    lazy.append(m)
    return top, lazy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    files = tracked_files()
    stdlib = sys.stdlib_module_names
    local = local_module_names()
    declared = declared_packages()

    vA1, vA2, vB1, vB2, vC1, vC2 = [], [], [], [], [], []
    third_party_table = []
    lazy_info = []

    # ── A1 真实企业名 ──
    for rel, ln, kw, txt in scan_text_patterns(files, BANNED_COMPANIES, "company"):
        vA1.append({"file": rel, "line": ln, "keyword": kw, "text": txt})
    # 目录/文件名本身含关键词也算
    for rel in files:
        for kw in BANNED_COMPANIES:
            if kw.lower() in rel.lower():
                vA1.append({"file": rel, "line": 0, "keyword": kw, "text": "(路径名)"})
                break

    # ── A2 内部路径 ──
    for rel, ln, seg, txt in scan_internal_paths(files):
        vA2.append({"file": rel, "line": ln, "path": seg, "text": txt})

    # ── B/C import 检查 ──
    for rel in sorted(iter_py_files()):
        top, lazy = parse_imports(rel)
        for mod, ln in top:
            if mod in stdlib or mod in local:
                continue
            # 开源引擎黑名单
            if any(mod == e or mod.startswith(e + "_") for e in OPEN_SOURCE_ENGINES):
                vB1.append({"file": rel, "line": ln, "module": mod})
                continue
            if mod in declared or mod in PKG_ALIAS.get(mod, set()):
                third_party_table.append({"file": rel, "line": ln, "module": mod, "declared": True})
            elif mod in declared:
                third_party_table.append({"file": rel, "line": ln, "module": mod, "declared": True})
            else:
                third_party_table.append({"file": rel, "line": ln, "module": mod, "declared": False})
                vC1.append({"file": rel, "line": ln, "module": mod})
                vB2.append({"file": rel, "line": ln, "module": mod})
        # C2 核心文件纯标准库
        base = os.path.basename(rel)
        if base in CORE_ENGINE:
            for mod, ln in top:
                if mod not in stdlib and mod not in local:
                    vC2.append({"file": rel, "line": ln, "module": mod})
        # INFO: 惰性 import(函数/try 内)命中开源引擎黑名单 —— 不计违规, 供人工确认
        for mod, ln in lazy:
            if any(mod == e or mod.startswith(e + "_") for e in OPEN_SOURCE_ENGINES):
                lazy_info.append({"file": rel, "line": ln, "module": mod})

    # ── 报告 ──
    print("=" * 76)
    print("开源边界自检报告  check_boundary.py")
    print("=" * 76)
    print(f"仓库根      : {ROOT}")
    print(f"扫描文件数  : {len(files)} (git 跟踪)")
    print(f"已声明依赖  : {sorted(declared)}")
    print()

    def dump(title, items, fmt):
        print("-" * 76)
        print(f"{title}: {len(items)}")
        print("-" * 76)
        for it in items:
            print("  " + fmt(it))
        print()

    dump("A1 真实企业名/客户名命中", vA1, lambda x: f"{x['file']}:{x['line']}  [{x['keyword']}]  {x['text']}")
    dump("A2 本机/内部绝对路径命中", vA2, lambda x: f"{x['file']}:{x['line']}  {x['path']}")
    dump("B1 import 开源引擎", vB1, lambda x: f"{x['file']}:{x['line']}  import {x['module']}")
    dump("B2 import 不存在的模块", vB2, lambda x: f"{x['file']}:{x['line']}  import {x['module']}")
    dump("C1 未声明的第三方依赖(核心路径)", vC1, lambda x: f"{x['file']}:{x['line']}  import {x['module']}")
    dump("C2 核心引擎文件引入了第三方", vC2, lambda x: f"{x['file']}:{x['line']}  import {x['module']}")

    decl = [t for t in third_party_table if t["declared"]]
    undecl = [t for t in third_party_table if not t["declared"]]
    print("-" * 76)
    print(f"[参考] 模块级第三方 import 共 {len(third_party_table)} 处 (已声明 {len(decl)} / 未声明 {len(undecl)})")
    print("-" * 76)
    for t in sorted(third_party_table, key=lambda x: (not x["declared"], x["module"])):
        flag = "已声明" if t["declared"] else "未声明!"
        print(f"  [{flag}] {t['file']}:{t['line']}  {t['module']}")
    print()
    print("-" * 76)
    print("[参考] 核心引擎文件纯标准库核对 (=%d 个)" % len(CORE_ENGINE))
    print("-" * 76)
    core_present = [c for c in CORE_ENGINE if os.path.exists(os.path.join(ROOT, "codes", c))]
    print(f"  检到核心文件 {len(core_present)}/{len(CORE_ENGINE)}; 违规 {len(vC2)}")
    print()
    print(f"[INFO] 惰性 import 命中开源引擎黑名单(函数/try 内, 不计违规): {len(lazy_info)}")
    for it in lazy_info:
        print(f"  {it['file']}:{it['line']}  import {it['module']}")

    total = len(vA1) + len(vA2) + len(vB1) + len(vB2) + len(vC1) + len(vC2)
    print()
    print("=" * 76)
    if total == 0:
        print("结论: 边界自检通过 (违规 0)")
    else:
        print(f"结论: 存在违规 {total} 处 —— "
              f"A1={len(vA1)} A2={len(vA2)} B1={len(vB1)} B2={len(vB2)} C1={len(vC1)} C2={len(vC2)}")
    print("=" * 76)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"A1": vA1, "A2": vA2, "B1": vB1, "B2": vB2,
                       "C1": vC1, "C2": vC2, "third_party": third_party_table},
                      fh, ensure_ascii=False, indent=2)
        print(f"(JSON 已写出: {args.json})")

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
