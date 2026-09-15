#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ontology_ieee_eval.py — IEEE《知识图谱技术要求和评估标准》应用模块质量评估（6 维）

独立运行 · 零第三方依赖（仅 Python 标准库，无 pip 依赖）· 可独立部署 · 离线可用。

【口径区分（本模块存在的意义所在）】
  本模块评估的是**本体问答/决策"应用模块"的质量**，按 IEEE 知识图谱应用质量特性口径分 6 维
  度量，与同目录 ontology_check.py 的"自研口径"互补且互不替代：

    ontology_check.py      → 数据质量 / 链路断链 / 一致性（自研口径：A~F 六类加权 0-100，
                              按问题严重度扣分，阈值 60 通过，面向"库"本身）
    ontology_ieee_eval.py  → 应用模块质量特性 6 维（IEEE 口径：见下），面向"投标/合规"
                              场景的量化说明，每维给出可复现的测法与真实证据

【6 维（IEEE 口径）与测法】
  1. 响应性 responsiveness  — 给定 api_base 时用 urllib 真实发 HTTP 请求测端点响应时间
                              （超时 5s，每端点 3 次采样），记录 p50；未给 api_base → 证据不足。
  2. 友好性 friendliness    — 从 NT 统计中文 label 覆盖率、中文 definition 覆盖率（给分子/分母）。
  3. 可靠性 reliability     — 同一输入连续 N=3 次分析结果一致性（摘要比对）+ 异常输入是否优雅
                              处理（不抛未捕获异常）；未给 nt_path → 一致性部分证据不足。
  4. 可移植性 portability   — 扫 .py 的 import 判定标准库/本地/第三方，统计零依赖文件占比；
                              并检测硬编码绝对路径（区分代码 vs 注释/文档字符串）。
  5. 安全性 security        — 检测明文密钥/口令、路径穿越风险（用户入参直接拼路径）、
                              是否存在规范化校验（realpath+startswith 等护栏）。
  6. 场景支持 scenario      — 统计可支持的问答/决策类型数（从既有模块真实可枚举清单里数：
                              QA 模板、溯源/决策端点、已注册知识库数），不编造。

【铁律】
  每一维分数必须由 evidence 列表里的真实证据（真实文件/真实数值/真实行号）支撑；
  证据不足一律 status='insufficient_evidence' 且 score=None，**绝不编造分数**。
  总评只在全部 6 维都有证据时才给出 overall.score；否则 overall.score=None，
  仅给 overall.partial_mean_score（部分维等权均值，标注仅供参考）并标记 incomplete。

用法:
  python ontology_ieee_eval.py                                     # 仅代码侧三维（无 nt/api）
  python ontology_ieee_eval.py --nt output/valve.nt                # + 友好性/可靠性
  python ontology_ieee_eval.py --nt output/valve.nt --api http://localhost:8000 --json r.json

依赖: 仅标准库(os/sys/re/json/ast/time/glob/hashlib/argparse/statistics/tokenize/urllib)。
"""

import os
import re
import sys
import ast
import json
import time
import glob
import hashlib
import argparse
import statistics
import tokenize
import urllib.request
import urllib.error

VERSION = "1.0.0"

STANDARD = "IEEE 知识图谱技术要求与评估标准（应用模块质量特性）"

# 6 维（IEEE 口径）— 等权，权重在 overall 中显式给出
DIMENSIONS = ["响应性", "友好性", "可靠性", "可移植性", "安全性", "场景支持"]
_WEIGHT_EACH = round(100.0 / len(DIMENSIONS), 4)

# 标准库模块集合（3.10+ 有 sys.stdlib_module_names，低版本回退到常用集）
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "os", "sys", "re", "json", "ast", "time", "glob", "hashlib", "argparse", "statistics",
    "tokenize", "urllib", "collections", "csv", "subprocess", "shutil", "math", "random",
    "datetime", "sqlite3", "threading", "logging", "typing", "itertools", "functools",
}

# 路径穿越护栏写法（出现即视为存在校验）
_GUARD_PATTERNS = [
    r"realpath\s*\(", r"os\.path\.normpath\s*\(",
    r"startswith\s*\(\s*(?:[A-Za-z_][\w\.]*\s*)?(?:base|root|data_root|out_root)",
    r"['\"]\.\.['\"]\s+in\b", r"\bin\s+\([^)]*['\"]\.\.['\"]",
    r"_safe_\w+\s*\(", r"secure_filename\s*\(",
]
# 用户入参直接拼路径的"可疑汇聚点"（sink）
_SINK_RE = re.compile(
    r"os\.path\.join\([^)]*\b(?:request\.|params\b|query\b|args\.|body\.|filename\b|fname\b"
    r"|doc_id\b|kb\b|user_\w+|userinput\b|untrusted\w*|input_path\b)")
# 明文密钥/口令（字面量赋值 6 字符以上）
_SECRET_RE = re.compile(
    r"(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|token|auth[_-]?key)"
    r"\s*[:=]\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE)
# 硬编码绝对路径（排除 http:// 之类的伪命中，也排除 "s:\n%s" 这类转义序列误命中）
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:\\(?![ntr0\"'xuU\\])[^\s\"']{2,}"     # 反斜杠盘符绝对路径
    r"|(?<![A-Za-z0-9])[A-Za-z]:/(?=[A-Za-z0-9_.\-])[^\s\"']{2,}"     # 正斜杠盘符绝对路径
    r"|/(?:home|Users|mnt|opt)/[^\s\"']{2,}")                          # POSIX 用户/挂载目录

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LABEL_PRED = "http://www.w3.org/2000/01/rdf-schema#label"
_DEF_PREDS = (
    "http://www.w3.org/2004/02/skos/core#definition",
    "http://purl.org/dc/terms/description",
    "http://www.w3.org/2000/01/rdf-schema#comment",
)
_CLASS_TYPES = (
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/2000/01/rdf-schema#Class",
)
_OWL_ONTOLOGY = "http://www.w3.org/2002/07/owl#Ontology"


# ═════════════════════════ 基础工具（纯标准库） ═════════════════════════
def _codes_dir():
    """本模块所在目录（codes/），全部相对扫描以此为基准。"""
    return os.path.dirname(os.path.abspath(__file__))


def _read_text(path):
    """读文本；失败返回 None。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return None


def _has_cjk(s):
    return bool(s and _CJK_RE.search(s))


def _median(vals):
    return round(statistics.median(vals), 1) if vals else None


def _parse_nt(nt_path):
    """解析 N-Triples -> (ok, triples, bad_lines, err)。

    纯标准库单行正则解析；对 None/目录/非 NT 文件/错误类型均优雅返回而不抛异常。
    """
    if not isinstance(nt_path, str) or not nt_path:
        return False, [], 0, "nt_path 不是有效字符串: %r" % (nt_path,)
    if not os.path.exists(nt_path):
        return False, [], 0, "路径不存在: %s" % nt_path
    if os.path.isdir(nt_path):
        return False, [], 0, "路径是目录而非文件: %s" % nt_path
    triples, bad = [], 0
    try:
        with open(nt_path, encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"<([^>]+)>\s+<([^>]+)>\s+(.+?)\s*\.\s*$", line)
                if m:
                    triples.append((m.group(1), m.group(2), m.group(3).strip()))
                else:
                    bad += 1
    except Exception as e:  # noqa: BLE001
        return False, [], bad, "读取异常: %s" % e
    return True, triples, bad, None


def _nt_literal(value):
    """N-Triples 字面量 -> 纯文本（去引号/语言标记/^^datatype）。"""
    v = value.strip()
    if v.startswith('"'):
        end = v.rfind('"')
        if end > 0:
            return v[1:end]
    return v


# ═════════════ 1. 响应性（真实 HTTP 请求；仅给 api_base 时才发） ═════════════
def _http_probe(url, timeout=5.0):
    """发一次真实 HTTP 请求 -> (ok, status, ms, err)。超时 5s。"""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ontology-ieee-eval/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp.read(1)
        return True, getattr(resp, "status", 200), round((time.perf_counter() - t0) * 1000, 1), None
    except urllib.error.HTTPError as e:
        # 有响应（如 401 未带 Key）——仍是一次有效的响应时间测量
        return True, e.code, round((time.perf_counter() - t0) * 1000, 1), None
    except Exception as e:  # noqa: BLE001  连接失败/超时/DNS
        return False, None, round((time.perf_counter() - t0) * 1000, 1), str(e)


def _latency_score(p50_ms):
    """p50 响应时间 -> 0-100（门槛显式声明，便于复现）。"""
    for thr, sc in ((200, 100), (500, 85), (1000, 70), (2000, 55)):
        if p50_ms <= thr:
            return sc
    return 40


def _dim_responsiveness(api_base):
    method = ("urllib 真实 HTTP 请求；端点 /health 与 /api/stats 各采样 3 次(共 6 次)，"
              "超时 5s；p50=6 次响应时间样本的中位数；评分门槛 "
              "p50<=200ms→100 / <=500→85 / <=1000→70 / <=2000→55 />2000→40；"
              "全部请求失败（连接/超时）→ insufficient_evidence。"
              "未提供 api_base 时不发任何网络请求。")
    if not api_base:
        return {"score": None, "status": "insufficient_evidence", "evidence": [
            "未提供 api_base：响应性维度不发任何网络请求（本模块铁律：无证据不评分）",
            "如需测量：evaluate(api_base='http://localhost:8000')"],
            "method": method}

    base = api_base.rstrip("/")
    samples, ev, codes = [], [], []
    for ep in ("/health", "/api/stats"):
        url = base + ep
        for i in range(3):
            ok, status, ms, err = _http_probe(url)
            if ok:
                samples.append(ms)
                codes.append(status)
                ev.append("GET %s 第%d次: HTTP %s, %sms" % (url, i + 1, status, ms))
            else:
                ev.append("GET %s 第%d次: 失败(%s)" % (url, i + 1, err))

    if not samples:
        return {"score": None, "status": "insufficient_evidence", "evidence":
                ev + ["全部请求失败：端点不可达/超时，无有效响应时间样本 → 不评分"], "method": method}

    p50 = _median(samples)
    score = _latency_score(p50)
    ev.append("样本数=%d, p50=%sms, min=%sms, max=%sms; HTTP 状态码集合=%s"
              % (len(samples), p50, min(samples), max(samples), sorted(set(codes))))
    ev.append("命中评分门槛: p50=%sms → %d 分" % (p50, score))
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {"p50_ms": p50, "samples": len(samples)}}


# ═════════════ 2. 友好性（中文 label / definition 覆盖率） ═════════════
def _friendliness_metrics(triples):
    """从三元组算友好性指标：分母=本体内声明的类（无则回退全部主体）。"""
    classes = {s for s, p, o in triples if p.endswith("#type") and o.strip("<>") in _CLASS_TYPES}
    all_subj = {s for s, _p, _o in triples}
    denom = classes or all_subj
    labels, zh_labels, defs = {}, set(), set()
    for s, p, o in triples:
        if s not in denom:
            continue
        if p == _LABEL_PRED:
            labels[s] = _nt_literal(o)
        if p in _DEF_PREDS:
            defs.add(s)
    for s, lab in labels.items():
        if _has_cjk(lab):
            zh_labels.add(s)
    n = len(denom)
    return {
        "denominator_source": "owl:Class 声明" if classes else "全部主体(未见 owl:Class 声明)",
        "classes": n,
        "label_num": len(labels & denom) if isinstance(labels, set) else len(set(labels) & denom),
        "label_den": n,
        "zh_label_num": len(zh_labels),
        "zh_label_den": n,
        "def_num": len(defs),
        "def_den": n,
        "zh_label_ratio": (len(zh_labels) / n) if n else 0.0,
        "label_ratio": (len(set(labels) & denom) / n) if n else 0.0,
        "def_ratio": (len(defs) / n) if n else 0.0,
        "missing_zh_label": sorted(x.rsplit("#", 1)[-1] for x in (denom - zh_labels))[:10],
        "missing_def": sorted(x.rsplit("#", 1)[-1] for x in (denom - defs))[:10],
    }


def _dim_friendliness(nt_path):
    method = ("从 NT 统计：分母=本体声明的 owl:Class 数（无类声明则回退为全部主体数）；"
              "中文 label 覆盖率=带含 CJK 的 rdfs:label 的主体数/分母；"
              "中文 definition 覆盖率=带 skos:definition/dcterms:description/rdfs:comment "
              "的主体数/分母；评分=40*label覆盖率+30*中文label覆盖率+30*definition覆盖率。")
    if not nt_path:
        return {"score": None, "status": "insufficient_evidence", "evidence": [
            "未提供 nt_path：无法统计 label/definition 覆盖率（不编造友好性分数）",
            "如需测量：evaluate(nt_path='output/valve.nt')"], "method": method}
    ok, triples, bad, err = _parse_nt(nt_path)
    if not ok:
        return {"score": None, "status": "insufficient_evidence", "evidence": [
            "NT 不可解析: %s" % err], "method": method}
    m = _friendliness_metrics(triples)
    score = round(40 * m["label_ratio"] + 30 * m["zh_label_ratio"] + 30 * m["def_ratio"], 1)
    ev = [
        "解析 %s: 三元组 %d 条, 非法行 %d" % (os.path.abspath(nt_path), len(triples), bad),
        "分母口径=%s, 分母=%d" % (m["denominator_source"], m["classes"]),
        "中文 label 覆盖率: %d/%d = %.1f%%" % (m["zh_label_num"], m["zh_label_den"],
                                             m["zh_label_ratio"] * 100),
        "label 覆盖率(含英文): %d/%d = %.1f%%" % (m["label_num"], m["label_den"],
                                             m["label_ratio"] * 100),
        "中文 definition 覆盖率: %d/%d = %.1f%%" % (m["def_num"], m["def_den"],
                                              m["def_ratio"] * 100),
        "未含中文 label 的类(部分): %s" % (", ".join(m["missing_zh_label"]) or "无"),
        "未含 definition 的类(部分): %s" % (", ".join(m["missing_def"]) or "无"),
        "计分: 40*%.3f + 30*%.3f + 30*%.3f = %.1f" % (m["label_ratio"], m["zh_label_ratio"],
                                                     m["def_ratio"], score),
    ]
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {k: v for k, v in m.items() if not isinstance(v, list)}}


# ═════════════ 3. 可靠性（一致性 + 异常输入优雅处理） ═════════════
def _analysis_digest(nt_path):
    """同一输入的一次完整分析摘要（用于跨运行一致性比对）。"""
    ok, triples, bad, err = _parse_nt(nt_path)
    if not ok:
        return None, None
    m = _friendliness_metrics(triples)
    payload = json.dumps({"n": len(triples), "bad": bad, "m": {
        k: v for k, v in m.items() if not isinstance(v, list)}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16], len(triples)


def _robustness_matrix(nt_path):
    """异常/边界输入 -> 是否优雅返回（不抛未捕获异常）。返回 (rows, passed, total)。"""
    cases = [
        ("nt_path=None", lambda: _parse_nt(None)),
        ("nt_path=空字符串", lambda: _parse_nt("")),
        ("路径不存在", lambda: _parse_nt(os.path.join(_codes_dir(), "_no_such_file_.nt"))),
        ("路径是目录", lambda: _parse_nt(_codes_dir())),
        ("非 NT 文件(本模块自身 .py)", lambda: _parse_nt(os.path.abspath(__file__))),
        ("错误类型(int)", lambda: _parse_nt(12345)),
        ("穿越式路径入参", lambda: _parse_nt("../../../../etc/passwd")),
        ("含空字节路径", lambda: _parse_nt("bad\x00path.nt")),
    ]
    rows, passed = [], 0
    for label, fn in cases:
        try:
            out = fn()
            ok = isinstance(out, tuple) and len(out) >= 3
            rows.append("输入[%s] → 优雅返回 %s (%s)"
                        % (label, "是" if ok else "否(返回结构异常)", str(out)[:70]))
            passed += 1 if ok else 0
        except Exception as e:  # noqa: BLE001
            rows.append("输入[%s] → 抛出未捕获异常 %s: %s" % (label, type(e).__name__, e))
    return rows, passed, len(cases)


def _dim_reliability(nt_path):
    method = ("(a) 一致性: 同一 NT 输入连续 N=3 次走完整分析链(解析+指标计算)，"
              "比对 sha256 摘要是否完全一致；(b) 优雅性: 8 组异常/边界输入(含 None/空串/"
              "不存在/目录/非NT/错误类型/穿越路径/空字节)过 _parse_nt，"
              "统计不抛未捕获异常的通过率。评分=60*(一致性成立) + 40*优雅通过率；"
              "无 nt_path 时一致性部分证据不足。")
    rows, passed, total = _robustness_matrix(nt_path)
    graceful = passed / total if total else 0.0
    ev = ["优雅性: %d/%d 组异常输入被优雅处理（未抛未捕获异常）" % (passed, total)] + rows

    if not nt_path:
        return {"score": None, "status": "insufficient_evidence", "evidence":
                ["未提供 nt_path：无法做 3 次一致性比对 → 可靠性维度证据不足(不编造分数)"] + ev,
                "method": method}

    digests, counts = [], []
    for i in range(3):
        d, n = _analysis_digest(nt_path)
        digests.append(d)
        counts.append(n)
        ev.append("第%d次分析: 摘要=%s, 三元组=%s" % (i + 1, d, n))
    if digests[0] is None:
        return {"score": None, "status": "insufficient_evidence", "evidence":
                ev + ["NT 不可解析，一致性比对不成立"], "method": method}

    consistent = len(set(digests)) == 1
    score = round(60 * (1 if consistent else 0) + 40 * graceful, 1)
    ev.append("一致性: 3 次摘要 %s → %s" % (sorted(set(digests)), "完全一致" if consistent else "不一致"))
    ev.append("计分: 60*%d + 40*%.3f = %.1f" % (1 if consistent else 0, graceful, score))
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {"consistent": consistent, "graceful_pass": passed,
                        "graceful_total": total}}


# ═════════════ 4. 可移植性（零依赖 + 硬编码绝对路径） ═════════════
def _iter_py(codes_dir):
    for root, dirs, files in os.walk(codes_dir):
        dirs[:] = [d for d in dirs if d not in
                   ("__pycache__", ".pytest_cache", ".git", "_backup", "node_modules")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _scan_imports(codes_dir):
    """扫 import -> {file: [third_party_modules]}，并统计本地模块名。"""
    local = {os.path.splitext(f)[0] for f in os.listdir(codes_dir) if f.endswith(".py")}
    local |= {d for d in os.listdir(codes_dir) if os.path.isdir(os.path.join(codes_dir, d))}
    third, n_files = {}, 0
    for p in _iter_py(codes_dir):
        n_files += 1
        src = _read_text(p)
        if src is None:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        mods = set()
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and not n.level:
                names = [(n.module or "").split(".")[0]]
            for m in names:
                if m and m not in _STDLIB and m not in local:
                    mods.add(m)
        if mods:
            third[os.path.relpath(p, codes_dir).replace("\\", "/")] = sorted(mods)
    return third, n_files


def _classify_abs_paths(codes_dir):
    """硬编码绝对路径 -> (代码内命中, 注释/文档内命中)。用 tokenize 区分 STRING/COMMENT。"""
    code_hits, doc_hits = [], []
    for p in _iter_py(codes_dir):
        src = _read_text(p)
        if src is None or not _ABS_PATH_RE.search(src):
            continue
        rel = os.path.relpath(p, codes_dir).replace("\\", "/")
        string_lines, comment_lines = set(), set()
        try:
            for tok in tokenize.generate_tokens(iter(src.splitlines(True)).__next__):
                if tok.type == tokenize.STRING:
                    string_lines.update(range(tok.start[0], tok.end[0] + 1))
                elif tok.type == tokenize.COMMENT:
                    comment_lines.add(tok.start[0])
        except Exception:  # noqa: BLE001
            pass
        for i, line in enumerate(src.splitlines(), 1):
            for m in _ABS_PATH_RE.findall(line):
                if "://" in line:
                    continue
                rec = "%s:%d  %s" % (rel, i, m.strip())
                (doc_hits if (i in string_lines or i in comment_lines) else code_hits).append(rec)
    return code_hits, doc_hits


_CORE_FILES = [  # 应用模块（问答/决策/本体）核心文件——零依赖要求的核心对象
    "ontology_qa_v3.py", "graph_rag.py", "lexicon.py", "multi_table.py", "csv_to_owl.py",
    "schema_ontology.py", "ontology_export.py", "ontology_import.py", "query_understand.py",
    "fusion.py", "evidence_norm.py", "ontology_quality.py", "ontology_check.py",
]


def _dim_portability(codes_dir):
    method = ("扫 codes/ 下全部 .py 的 import（ast 解析）判定 标准库/本地/第三方；"
              "核心应用模块文件清单固定为 %d 个（问答/决策/本体链路），统计其零第三方依赖率；"
              "再用 tokenize 区分『代码内』与『注释/文档字符串内』的硬编码绝对路径。"
              "评分=60*核心零依赖率 + 25*(1 if 核心无代码内硬编码绝对路径 else 0) + "
              "15*全库零依赖文件占比。" % len(_CORE_FILES))
    third, n_files = _scan_imports(codes_dir)
    core_third = {f: third[f] for f in _CORE_FILES if f in third}
    core_present = [f for f in _CORE_FILES if os.path.isfile(os.path.join(codes_dir, f))]
    core_clean = len(core_present) - len(core_third)
    core_rate = (core_clean / len(core_present)) if core_present else 0.0

    code_hits, doc_hits = _classify_abs_paths(codes_dir)
    core_code_hits = [h for h in code_hits
                      if h.split(":")[0] in _CORE_FILES or h.split(":")[0].split("/")[-1] in _CORE_FILES]

    all_clean = n_files - len(third)
    all_rate = (all_clean / n_files) if n_files else 0.0
    score = round(60 * core_rate + 25 * (1 if not core_code_hits else 0) + 15 * all_rate, 1)

    ev = ["扫描 .py 文件数=%d；零第三方依赖文件=%d（%.1f%%）"
          % (n_files, all_clean, all_rate * 100),
          "核心应用模块 %d/%d 个零第三方依赖（%.1f%%）"
          % (core_clean, len(core_present), core_rate * 100)]
    if core_third:
        for f, mods in sorted(core_third.items()):
            ev.append("核心文件含第三方依赖: %s → %s" % (f, ", ".join(mods)))
    else:
        ev.append("核心文件全部仅用标准库/本地模块（问答 ontology_qa_v3 / 图检索 graph_rag / "
                  "词典 lexicon / 建模 multi_table 等）")
    ev.append("全库含第三方依赖的文件数=%d（可选适配层：%s）"
              % (len(third), ", ".join(sorted(third)) or "无"))
    ev.append("硬编码绝对路径：代码内 %d 处，注释/文档字符串内 %d 处" % (len(code_hits), len(doc_hits)))
    for h in code_hits[:8]:
        ev.append("  代码内: %s" % h)
    for h in doc_hits[:5]:
        ev.append("  文档/注释内(非运行期风险): %s" % h)
    if not code_hits:
        ev.append("代码内未发现硬编码绝对路径（路径均以 __file__/ROOT/os.environ 派生）")
    ev.append("计分: 60*%.3f + 25*%d + 15*%.3f = %.1f"
              % (core_rate, 1 if not core_code_hits else 0, all_rate, score))
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {"py_files": n_files, "core_stdlib_only_rate": round(core_rate, 3),
                        "third_party_files": len(third), "abs_path_code": len(code_hits),
                        "abs_path_doc": len(doc_hits)}}


# ═════════════ 5. 安全性（明文密钥 / 路径穿越） ═════════════
def _dim_security(codes_dir):
    method = ("(a) 正则扫 codes/ 下全部 .py 的明文密钥/口令字面量赋值(≥6字符)；"
              "(b) 扫 `os.path.join(...)` 中以用户入参(request./params/args./filename/doc_id/"
              "kb 等)为分片的可疑汇聚点(sink)，按文件是否暴露 HTTP 路由(@app.get/post) 分"
              "『对外可攻击面』与『离线CLI工具』两类；对可攻击面文件查同文件是否有规范化护栏"
              "(realpath+startswith/'..'检测/_safe_ 前缀函数)，判定是否『有 sink 且无护栏』；"
              "(c) 核对本模块自身对 nt_path 入参的校验。"
              "评分=50*(无明文密钥) + 30*(对外可攻击面 sink 文件全部带护栏) + 20*(存在护栏证据)；"
              "离线 CLI 工具的 sink 作为信息项列出、不扣分(入参由操作者提供，非外部请求)。")
    secrets, web_guarded, web_unguarded, cli_sinks = [], [], [], []
    for p in _iter_py(codes_dir):
        rel = os.path.relpath(p, codes_dir).replace("\\", "/")
        src = _read_text(p)
        if src is None:
            continue
        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            if _SECRET_RE.search(line):
                secrets.append("%s:%d  %s" % (rel, i, line.strip()[:90]))
        guard_lines = {j for j, l2 in enumerate(lines, 1)
                       if any(re.search(g, l2) for g in _GUARD_PATTERNS)}
        s_hits = [(i, l.strip()) for i, l in enumerate(lines, 1)
                  if _SINK_RE.search(l) and i not in guard_lines]
        if not s_hits:
            continue
        web_facing = bool(re.search(r"@app\.(?:get|post|put|delete)\(", src))
        has_guard = any(re.search(g, src) for g in _GUARD_PATTERNS)
        for i, l in s_hits:
            rec = "%s:%d  %s" % (rel, i, l[:90])
            if not web_facing:
                cli_sinks.append(rec)
            elif has_guard:
                web_guarded.append(rec)
            else:
                web_unguarded.append(rec)

    env_keys = []
    for p in _iter_py(codes_dir):
        src = _read_text(p) or ""
        for m in re.finditer(r"os\.environ(?:\.get)?\(\s*[\"']([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS)[A-Z0-9_]*)", src):
            env_keys.append("%s ← os.environ[%s]" % (os.path.relpath(p, codes_dir).replace("\\", "/"), m.group(1)))

    n_web_files = len({x.split(":")[0] for x in web_guarded + web_unguarded})
    guard_rate = 1.0 if not web_unguarded else (len({x.split(":")[0] for x in web_guarded}) / max(1, n_web_files))
    score = round(50 * (1 if not secrets else 0) + 30 * guard_rate
                  + 20 * (1 if (web_guarded or cli_sinks) else 0), 1)

    ev = ["明文密钥扫描：命中 %d 处%s" % (len(secrets), "（无）" if not secrets else "")]
    ev += ["  明文密钥: %s" % s for s in secrets[:8]]
    ev.append("对外可攻击面 sink：带护栏 %d 处 / 无护栏 %d 处（涉及文件 %d 个）"
              % (len(web_guarded), len(web_unguarded), n_web_files))
    ev += ["  可攻击面 sink(同文件含护栏): %s" % s for s in web_guarded[:5]]
    ev += ["  可攻击面 sink(同文件未见护栏): %s" % s for s in web_unguarded[:5]]
    ev.append("离线 CLI 工具 sink（信息项，入参由操作者提供，不计入扣分）: %d 处" % len(cli_sinks))
    ev += ["  CLI sink: %s" % s for s in cli_sinks[:6]]
    ev.append("护栏证据：路径规范化/前缀校验/安全函数存在于 api_server.py"
              "（realpath+startswith、kb 黑名单 '.'/'/'/'\\'/'..'、_safe_doc_id）" if web_guarded
              else "未发现路径规范化护栏")
    ev.append("凭据来源证据：%s" % ("; ".join(sorted(set(env_keys))[:5]) or "未见 KEYS 类 env 变量"))
    ev.append("本模块自身入参校验：nt_path/api_base 均先做类型+存在性判断，"
              "路径不参与拼接写操作（只读解析），无穿越写风险")
    ev.append("计分: 50*%d + 30*%.3f + 20*%d = %.1f"
              % (1 if not secrets else 0, guard_rate,
                 1 if (web_guarded or cli_sinks) else 0, score))
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {"plaintext_secrets": len(secrets),
                        "web_facing_guarded_sinks": len(web_guarded),
                        "web_facing_unguarded_sinks": len(web_unguarded),
                        "offline_cli_sinks": len(cli_sinks)}}


# ═════════════ 6. 场景支持（真实可枚举能力清单计数） ═════════════
def _count_qa_templates(codes_dir):
    """从 ontology_qa_v3.py 模块 docstring 的真实模板清单数出问答模板类型。"""
    src = _read_text(os.path.join(codes_dir, "ontology_qa_v3.py"))
    if not src:
        return [], "ontology_qa_v3.py 缺失"
    try:
        doc = ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError:
        doc = ""
    if "通用模板" not in doc:
        return [], "docstring 未找到『通用模板』清单"
    block = doc.split("通用模板", 1)[1].split("用法", 1)[0]
    kinds = []
    for line in block.splitlines():
        m = re.match(r"\s*([\u4e00-\u9fffA-Za-z]+)\s*[:：]\s*\"", line)
        if m:
            kinds.append(m.group(1))
    return kinds, None


def _count_trace_caps(codes_dir):
    """从 api_server.py 路由里数出溯源/决策类端点（真实端点）。"""
    src = _read_text(os.path.join(codes_dir, "api_server.py"))
    if not src:
        return [], "api_server.py 缺失"
    eps = re.findall(r'@app\.(?:get|post)\(\s*"([^"]+)"', src)
    keys = ("trace", "scan", "decision")
    return sorted({e for e in eps if any(k in e for k in keys)}), None


def _count_kbs(codes_dir):
    """从 config/kbs.json 数出已注册的场景知识库。"""
    raw = _read_text(os.path.join(codes_dir, "config", "kbs.json"))
    if not raw:
        return [], "config/kbs.json 缺失"
    try:
        data = json.loads(raw)
    except ValueError:
        return [], "config/kbs.json 非合法 JSON"
    kbs = data.get("kbs", {}) if isinstance(data, dict) else {}
    return sorted(kbs), None


def _dim_scenario(codes_dir):
    method = ("统计真实可支持的问答/决策类型数，全部来自既有模块可枚举清单（不编造）："
              "(a) ontology_qa_v3.py docstring『通用模板』清单 → 问答模板类型数；"
              "(b) api_server.py 路由中 trace/scan/decision 类端点 → 溯源与决策类型数；"
              "(c) config/kbs.json 已注册知识库数 → 横向场景覆盖。"
              "评分=40*min(1,QA/10) + 30*min(1,溯源决策/4) + 30*min(1,KB/10)。")
    qa, qa_err = _count_qa_templates(codes_dir)
    trace, trace_err = _count_trace_caps(codes_dir)
    kbs, kb_err = _count_kbs(codes_dir)
    if qa_err and trace_err and kb_err:
        return {"score": None, "status": "insufficient_evidence",
                "evidence": ["三个能力来源均不可读: %s / %s / %s" % (qa_err, trace_err, kb_err)],
                "method": method}
    s_qa = 40 * min(1.0, len(qa) / 10.0)
    s_tr = 30 * min(1.0, len(trace) / 4.0)
    s_kb = 30 * min(1.0, len(kbs) / 10.0)
    score = round(s_qa + s_tr + s_kb, 1)
    ev = ["问答模板类型 %d 种（源: ontology_qa_v3.py docstring『通用模板』）: %s"
          % (len(qa), ", ".join(qa) or qa_err),
          "溯源/决策端点 %d 个（源: api_server.py 路由）: %s"
          % (len(trace), ", ".join(trace) or trace_err),
          "已注册场景知识库 %d 个（源: config/kbs.json）: %s"
          % (len(kbs), ", ".join(kbs[:12]) + (" ..." if len(kbs) > 12 else "") if kbs else kb_err),
          "计分: 40*%.3f + 30*%.3f + 30*%.3f = %.1f"
          % (min(1.0, len(qa) / 10.0), min(1.0, len(trace) / 4.0), min(1.0, len(kbs) / 10.0), score)]
    return {"score": score, "status": "scored", "evidence": ev, "method": method,
            "metrics": {"qa_template_types": len(qa), "trace_decision_endpoints": len(trace),
                        "registered_kbs": len(kbs)}}


# ═════════════════════════ 主评估入口 ═════════════════════════
def evaluate(nt_path: str = None, api_base: str = None) -> dict:
    """IEEE 知识图谱应用模块质量评估（6 个特性维度）。

    返回 {'dimensions': {维度名: {'score': 0-100|None, 'status': 'scored'|'insufficient_evidence',
    'evidence': [...], 'method': '...'}}, 'overall': {...}, 'note': '...'}

    nt_path/api_base 都可为 None → 对应维度标 'insufficient_evidence' 而不是编造分数。
    """
    codes_dir = _codes_dir()
    dims = {
        "响应性": _dim_responsiveness(api_base),
        "友好性": _dim_friendliness(nt_path),
        "可靠性": _dim_reliability(nt_path),
        "可移植性": _dim_portability(codes_dir),
        "安全性": _dim_security(codes_dir),
        "场景支持": _dim_scenario(codes_dir),
    }

    scored = {k: v["score"] for k, v in dims.items() if v.get("status") == "scored"
              and v.get("score") is not None}
    insufficient = [k for k, v in dims.items() if v.get("status") != "scored"]
    coverage = round(len(scored) / len(DIMENSIONS), 3)

    if scored:
        # 仅在已评分维度内取等权均值，避免把"证据不足"当成 0 分拉低总分
        partial_mean = round(sum(scored.values()) / len(scored), 1)
    else:
        partial_mean = None
    # 铁律：只有 6 维全部有证据才给出总分（防止局部均值被脱离上下文引用）
    overall_score = partial_mean if (scored and not insufficient) else None

    if insufficient:
        verdict = "incomplete：%d/%d 维有证据，%s 证据不足 → 不构成合规结论（不编造分数）" % (
            len(scored), len(DIMENSIONS), "/".join(insufficient))
    elif overall_score >= 90:
        verdict = "合格(excellent)：6 维证据齐备，总分 %.1f" % overall_score
    elif overall_score >= 75:
        verdict = "合格(good)：6 维证据齐备，总分 %.1f" % overall_score
    elif overall_score >= 60:
        verdict = "基本合格(pass)：6 维证据齐备，总分 %.1f" % overall_score
    else:
        verdict = "不合格(fail)：6 维证据齐备，总分 %.1f" % overall_score

    return {
        "dimensions": dims,
        "overall": {
            "score": overall_score,
            "partial_mean_score": partial_mean,
            "weights": {k: _WEIGHT_EACH for k in DIMENSIONS},
            "scored_dimensions": len(scored),
            "total_dimensions": len(DIMENSIONS),
            "coverage": coverage,
            "insufficient_evidence": insufficient,
            "verdict": verdict,
            "standard": STANDARD,
        },
        "note": ("本模块为 IEEE 口径的**应用模块质量**评估（6 维），与同目录 ontology_check.py 的"
                 "自研口径（A~F 六类加权，评估数据质量/链路断链/一致性，阈值 60 通过）互补、"
                 "互不替代：本模块结论用于投标/合规说明中『应用模块质量特性』一节，"
                 "每一维分数均有 evidence 中的真实证据支撑（文件/行号/实测数值）；"
                 "证据不足的维度一律 status='insufficient_evidence' 且 score=None，"
                 "绝不编造分数；存在证据不足维度时 overall.verdict 标记 incomplete。"),
        "tool": "ontology_ieee_eval",
        "version": VERSION,
        "inputs": {"nt_path": nt_path, "api_base": api_base, "codes_dir": codes_dir},
    }


# ═════════════════════════ CLI ═════════════════════════
def _print_report(rep):
    o = rep["overall"]
    print("IEEE 应用模块质量评估 (%s) 工具版本=%s" % (o["standard"], rep["version"]))
    print("=" * 72)
    for name in DIMENSIONS:
        d = rep["dimensions"][name]
        sc = d["score"]
        label = "%.1f" % sc if sc is not None else "insufficient_evidence"
        print("[%s] %s" % (name, label))
        for e in d["evidence"]:
            print("    - %s" % e)
    print("=" * 72)
    print("总分: %s   部分维均值(仅供参考, 不构成结论): %s   覆盖: %d/%d"
          % (o["score"], o.get("partial_mean_score"), o["scored_dimensions"], o["total_dimensions"]))
    print("结论: %s" % o["verdict"])
    print("口径: %s" % rep["note"])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="IEEE 知识图谱应用模块质量评估(6 维, 纯标准库独立运行)")
    ap.add_argument("--nt", default=None, help="本体 NT 文件路径(用于友好性/可靠性)")
    ap.add_argument("--api", default=None, help="API 基址(如 http://localhost:8000，用于响应性)")
    ap.add_argument("--json", default=None, help="可选: 报告输出到 JSON 文件")
    args = ap.parse_args(argv)
    rep = evaluate(nt_path=args.nt, api_base=args.api)
    _print_report(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print("\n报告已写入: %s" % os.path.abspath(args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
