#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ontology_check.py — 工厂本体库「本体检查」能力（核查全库：数据质量 / 链路断链 / 一致性）

独立运行 · 零第三方依赖（仅 Python 标准库，无 pip 依赖）· 可独立部署。

能力（5 类核查，输出 0-100 分 + 问题清单）：
  A. 链路断链(chain)   — 本体链路完整性: Web桥接↔套件脚本↔CSV↔NT↔词典↔决策，
                         并对代表知识库做一次真实「CSV→本体」多表构建冒烟测试，
                         验证数据流 CSV→本体 无断链。
  B. CSV 数据质量       — 全库 CSV: 可读性/空表/表头/空主键/重复主键/空单元格。
  C. NT 本体质量        — 本体产物 output/*.nt（本地存在时）: 解析合法性/空文件/悬空引用；
                         并检查冒烟构建产物的悬空引用。
  D. 词典一致性(lexicon) — industrial_dict/*.json: JSON 合法性/空词典/必需键/空中文键/空英文值/英文值重复。
  E. 本体一致性         — config/relations.json 合法性 + 对象属性 target_class 存在性 + 本体类声明确认。

计分: 满分 100，按类别权重加权，各类别内部按问题严重度扣分。
通过阈值默认 60（--threshold 可调），exit code 0=通过(score>=threshold 且无致命问题)。

用法:
  python ontology_check.py                 # 检查当前仓库(脚本所在目录及其上级 repo 根)
  python ontology_check.py --dir <codes路径>
  python ontology_check.py --threshold 60 --json report.json
  python ontology_check.py --no-build      # 跳过真实构建冒烟测试(更快)

依赖: 仅标准库(os/sys/json/csv/re/glob/subprocess/collections/argparse)。
"""

import os
import sys
import re
import csv
import json
import glob
import shutil
import argparse
import subprocess
from collections import Counter

VERSION = "1.0.0"

# ── 类别权重（合计 100）─────────────────────────────
WEIGHTS = {
    "chain": 20,   # A 链路断链
    "csv": 25,     # B CSV 数据质量
    "nt": 20,      # C NT 本体质量
    "lexicon": 15, # D 词典一致性
    "consistency": 20,  # E 本体一致性
}

# 本体命名空间(与 csv_to_owl/multi_table 一致)
NS = "http://factory.example/ontology#"


# ═════════════════════════ 最小解析工具(纯标准库) ═════════════════════════
def _tracked_files(repo_root):
    """返回 git 已提交(入库)文件的绝对路径集合。

    用 `git ls-files` 确定"入库"文件，保证本地扫描与 CI(纯净检出)一致：
    未入库的临时产物(如 output/*.nt 运行时生成、data/*.csv 敏感数据)不计入"全库"。
    git 不可用时返回 None(调用方回退到扫描磁盘全部文件)。
    """
    try:
        r = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", cwd=repo_root,
                           timeout=60)
        if r.returncode != 0:
            return None
        return {os.path.normpath(os.path.join(repo_root, p)) for p in r.stdout.splitlines()}
    except Exception:  # noqa: BLE001
        return None


def _parse_csv(path):
    """读取 CSV -> (ok, headers, rows, err)。带 BOM 处理。"""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            headers = list(reader.fieldnames) if reader.fieldnames else []
        return True, headers, rows, None
    except Exception as e:  # noqa: BLE001
        return False, [], [], str(e)


def _parse_nt(path):
    """解析 N-Triples -> (ok, triples, err)。单行标准格式，忽略空行/注释。"""
    triples = []
    bad = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"<([^>]+)>\s+<([^>]+)>\s+(.+?)\s*\.\s*$", line)
                if not m:
                    bad += 1
                    continue
                triples.append((m.group(1), m.group(2), m.group(3).strip()))
        return True, triples, bad
    except Exception as e:  # noqa: BLE001
        return False, [], str(e)


def _is_entity_object(o):
    """对象是实体引用(而非字面量)。"""
    return o.startswith("<") and o.endswith(">")


# ═════════════════════════ A. 链路断链 ═════════════════════════
def _check_chain(codes_dir, repo_root, skip_build):
    """本体链路完整性 + 真实 CSV→本体 构建冒烟测试。"""
    issues = []  # (severity, msg)
    links = []
    state = {"build_smoke": None}

    # 核心套件脚本(必选, 已提交)
    core = [
        ("run.py", "入口 run.py"),
        ("csv_to_owl.py", "单表建模 csv_to_owl.py"),
        ("multi_table.py", "多表建模 multi_table.py"),
        ("data_loader.py", "数据加载 data_loader.py"),
        ("lexicon.py", "词典 lexicon.py"),
        ("ontology_qa_v3.py", "问答 ontology_qa_v3.py"),
        ("graph_rag.py", "图检索 graph_rag.py"),
        ("api_server.py", "API server.py"),
        ("config/relations.json", "对象属性配置 relations.json"),
    ]
    for rel, label in core:
        p = os.path.join(codes_dir, rel)
        ok = os.path.isfile(p)
        links.append({"label": label, "rel": rel, "ok": ok})
        if not ok:
            issues.append(("critical", f"链路断: {label} 缺失 {p}"))

    # Web 桥接(开源版随仓提交)
    web_bridge = os.path.join(repo_root, "web", "server", "ontology.js")
    ok = os.path.isfile(web_bridge)
    links.append({"label": "Web 桥接 web/server/ontology.js", "rel": "web/server/ontology.js", "ok": ok})
    if not ok:
        issues.append(("critical", f"链路断: Web 桥接缺失 {web_bridge}"))

    # 词典目录(公共词典, 已提交)
    ind_dict = os.path.join(codes_dir, "industrial_dict")
    ok = os.path.isdir(ind_dict)
    links.append({"label": "公共词典 industrial_dict", "rel": "industrial_dict", "ok": ok})
    if not ok:
        issues.append(("critical", f"链路断: 词典目录缺失 {ind_dict}"))

    # 数据目录: 至少一个 data_* 知识库含 CSV
    kb_dirs = sorted(d for d in glob.glob(os.path.join(codes_dir, "data_*"))
                     if os.path.isdir(d) and os.path.basename(d) not in ("data_import.py",))
    n_csv = 0
    for d in kb_dirs:
        n_csv += len(glob.glob(os.path.join(d, "*.csv")))
    ok = n_csv > 0
    links.append({"label": f"知识库数据目录({len(kb_dirs)} 个, {n_csv} 张 CSV)",
                  "rel": "data_*", "ok": ok})
    if not ok:
        issues.append(("critical", "链路断: 未发现任何 data_* 知识库 CSV"))

    # 决策层(rules_engine) — 本地运行时组件; 未提交时不判致命
    decision_py = os.path.join(codes_dir, "decision", "rules_engine.py")
    decision_ok = os.path.isfile(decision_py)
    links.append({"label": "决策引擎 decision/rules_engine.py", "rel": "decision/rules_engine.py",
                  "ok": decision_ok})
    if not decision_ok:
        issues.append(("minor", "提示: 决策引擎未提交(CI 纯净检出不含决策层, 属运行时组件)"))

    # 真实「CSV→本体」多表构建冒烟测试: 证明数据流无断链
    if not skip_build:
        kb = "data_valve" if os.path.isdir(os.path.join(codes_dir, "data_valve")) \
            else (kb_dirs[0] if kb_dirs else None)
        if kb:
            csvs = sorted(glob.glob(os.path.join(codes_dir, kb, "*.csv")))
            tmp_out = os.path.join(codes_dir, f"._ontology_check_smoke.nt")
            rc = 1
            try:
                cmd = [sys.executable, "multi_table.py", tmp_out] + csvs
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="ignore", cwd=codes_dir,
                                   timeout=120)
                rc = r.returncode
            except Exception as e:  # noqa: BLE001
                issues.append(("critical", f"构建冒烟测试执行异常: {e}"))
            finally:
                if os.path.exists(tmp_out):
                    try:
                        os.remove(tmp_out)
                    except OSError:
                        pass
            state["build_smoke"] = {"kb": kb, "n_csv": len(csvs), "rc": rc}
            links.append({"label": f"真实构建冒烟测试({kb}, {len(csvs)} 张表)",
                          "rel": f"{kb}->NT", "ok": rc == 0})
            if rc != 0:
                issues.append(("critical",
                               f"链路断: CSV→本体 构建失败({kb}): rc={rc}"))
            else:
                issues.append(("info", f"构建冒烟通过: {kb} {len(csvs)} 表成功生成本体"))

    return {"issues": issues, "links": links, "state": state}


# ═════════════════════════ B. CSV 数据质量 ═════════════════════════
def _check_csv(codes_dir, tracked=None):
    issues = []
    stats = Counter()
    files = []
    for pat in ("data_*/*.csv", "data/*.csv"):
        files.extend(glob.glob(os.path.join(codes_dir, pat)))
    files = sorted({os.path.normpath(p) for p in files})
    if tracked is not None:
        files = [p for p in files if p in tracked]
    for p in files:
        name = os.path.relpath(p, codes_dir)
        ok, headers, rows, err = _parse_csv(p)
        if not ok:
            issues.append(("critical", f"[CSV] {name}: 读取失败 {err}")); stats["unreadable"] += 1
            continue
        if not rows:
            issues.append(("critical", f"[CSV] {name}: 空表(无数据行)")); stats["empty"] += 1
            continue
        if not headers or any(not str(c).strip() for c in headers):
            issues.append(("major", f"[CSV] {name}: 表头存在空列名")); stats["badheader"] += 1
        keycol = "id" if "id" in headers else (headers[0] if headers else "")
        is_join = ("batch_id" in headers and "raw_id" in headers)  # join 表主键非唯一属正常
        seen, emptypk, dup = set(), 0, set()
        for row in rows:
            v = str(row.get(keycol, "")).strip()
            if not v:
                emptypk += 1
            elif not is_join:
                if v in seen:
                    dup.add(v)
                seen.add(v)
        if emptypk:
            issues.append(("major", f"[CSV] {name}: {emptypk} 行缺主键 {keycol}")); stats["emptypk"] += 1
        if dup:
            issues.append(("major", f"[CSV] {name}: 重复主键 {sorted(dup)[:4]} 等")); stats["duppk"] += 1
        # 空单元格(轻微)
        empty_cells = sum(1 for row in rows for c in row.values()
                          if c is None or str(c).strip() == "")
        if empty_cells:
            issues.append(("minor", f"[CSV] {name}: {empty_cells} 个空单元格")); stats["emptycells"] += 1
    stats["files"] = len(files)
    return {"issues": issues, "stats": dict(stats)}


# ═════════════════════════ C. NT 本体质量 ═════════════════════════
def _nt_dangling(triples):
    """返回悬空实体引用列表: 对象是 factory 实体但未在任何 subject 出现、也非声明类。"""
    subjects = set()
    for s, _p, _o in triples:
        subjects.add(s)
    obj_refs = []
    for _s, _p, o in triples:
        if _is_entity_object(o):
            obj_refs.append(o.strip("<>"))
    dangling = set(o for o in obj_refs
                   if o not in subjects and o.startswith(NS))
    return sorted(dangling)


def _check_nt(codes_dir, smoke_dangling, tracked=None):
    issues = []
    stats = Counter()
    # 1) 入库本体产物 output/*.nt(运行时生成不提交; 纯净检出无产物属正常, 依赖构建冒烟)
    nt_files = sorted({os.path.normpath(p)
                       for p in glob.glob(os.path.join(codes_dir, "output", "*.nt"))})
    if tracked is not None:
        nt_files = [p for p in nt_files if p in tracked]
    for p in nt_files:
        name = os.path.relpath(p, codes_dir)
        ok, triples, bad = _parse_nt(p)
        if not ok:
            issues.append(("critical", f"[NT] {name}: 解析失败")); stats["unparseable"] += 1
            continue
        if not triples and bad == 0:
            issues.append(("major", f"[NT] {name}: 空本体(0 三元组)")); stats["empty"] += 1
            continue
        if bad:
            issues.append(("major", f"[NT] {name}: {bad} 行非法三元组")); stats["badline"] += 1
        dang = _nt_dangling(triples)
        if dang:
            issues.append(("major", f"[NT] {name}: {len(dang)} 个悬空实体引用 {dang[:4]}..."))
            stats["dangling_files"] += 1
            stats["dangling_refs"] += len(dang)
    stats["files"] = len(nt_files)
    # 2) 构建冒烟产物的悬空引用(若产生)
    if smoke_dangling:
        issues.append(("major", f"[NT] 构建冒烟产物: {len(smoke_dangling)} 个悬空实体引用 "
                                f"{smoke_dangling[:4]}..."))
        stats["smoke_dangling"] = len(smoke_dangling)
    return {"issues": issues, "stats": dict(stats)}


# ═════════════════════════ D. 词典一致性 ═════════════════════════
_REQUIRED_KEYS = ("description", "version", "industry", "type_cn2en")


def _check_lexicon(codes_dir):
    issues = []
    stats = Counter()
    files = sorted(glob.glob(os.path.join(codes_dir, "industrial_dict", "*.json")))
    for p in files:
        name = os.path.relpath(p, codes_dir)
        is_index = os.path.basename(p).lower() == "index.json"
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            issues.append(("critical", f"[词典] {name}: JSON 非法 {e}")); stats["invalid"] += 1
            continue
        if isinstance(data, list):
            if not data:
                issues.append(("critical", f"[词典] {name}: 空词典")); stats["empty"] += 1
            stats["entries"] += len(data)
            continue
        if not data:
            issues.append(("critical", f"[词典] {name}: 空词典")); stats["empty"] += 1
            continue
        # index.json 是词典清单/架构清单, 不做"必需键"检查(结构不同), 仅 JSON 合法即可
        if not is_index:
            missing = [k for k in _REQUIRED_KEYS if k not in data]
            if missing:
                issues.append(("major", f"[词典] {name}: 缺必需键 {missing}")); stats["missingkey"] += 1
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            en_vals = []
            for cn, en in v.items():
                if not str(cn).strip():
                    issues.append(("minor", f"[词典] {name}.{k}: 空中文键")); stats["emptycn"] += 1
                if not str(en).strip():
                    issues.append(("minor", f"[词典] {name}.{k}: 空英文值")); stats["emptyen"] += 1
                else:
                    en_vals.append(str(en))
            dup = {x for x in set(en_vals) if en_vals.count(x) > 1}
            if dup and k not in ("synonym_map", "material_synonyms", "standards"):
                # 同义词映射里"多中一英"属正常; 仅对 cn2en 词典提示
                issues.append(("minor", f"[词典] {name}.{k}: 英文值重复 {sorted(dup)[:3]}"))
                stats["dupen"] += len(dup)
    stats["files"] = len(files)
    return {"issues": issues, "stats": dict(stats)}


# ═════════════════════════ E. 本体一致性 ═════════════════════════
def _check_consistency(codes_dir, smoke_triples, smoke_tables=None):
    issues = []
    stats = Counter()
    rel_json = os.path.join(codes_dir, "config", "relations.json")
    if not os.path.isfile(rel_json):
        issues.append(("critical", "[一致性] config/relations.json 缺失"))
    else:
        try:
            with open(rel_json, encoding="utf-8") as f:
                rel = json.load(f)
            if not isinstance(rel, dict):
                issues.append(("critical", "[一致性] relations.json 顶层非对象"))
            else:
                n_tables = sum(1 for k, v in rel.items() if not k.startswith("_"))
                stats["relation_tables"] = n_tables
                smoke_tables = set(smoke_tables or [])
                # (tbl, target_class) 列表
                target_defs = []
                for tbl, cfg in rel.items():
                    if tbl.startswith("_"):
                        continue
                    for col, cc in (cfg.get("object_properties") or {}).items():
                        if not str(col).strip():
                            issues.append(("minor", f"[一致性] relations.json 空对象属性列"))
                            stats["empty_col"] += 1
                        tc = cc.get("target_class", "")
                        if not str(tc).strip():
                            issues.append(("major", f"[一致性] {tbl}.{col} 缺 target_class"))
                            stats["no_target"] += 1
                        else:
                            target_defs.append((tbl, tc))
                # 冒烟产物中的 target_class 类声明确认:
                # 仅当该 target_class 所属的表在本体构建(冒烟)中被实际建模时才核对,
                # 避免 relations.json 为其他数据流(如 equipment 流)配置的对象属性被误判。
                if smoke_triples and smoke_tables:
                    classes = {s for s, p, _o in smoke_triples
                               if p == "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
                               and "Class" in _o}
                    for tbl, tc in target_defs:
                        if tbl not in smoke_tables:
                            continue  # 该表未在冒烟构建中, 无法核对 -> 跳过
                        if NS + tc not in classes:
                            issues.append(("major", f"[一致性] 对象属性 target_class '{tc}' "
                                                     f"(表 {tbl}) 在构建本体中无类声明"))
                            stats["missing_class"] += 1
                        else:
                            stats["class_ok"] += 1
        except (json.JSONDecodeError, OSError) as e:
            issues.append(("critical", f"[一致性] relations.json 解析失败 {e}"))
    return {"issues": issues, "stats": dict(stats)}


# ═════════════════════════ 计分 ═════════════════════════
# 严重度扣分(每项); 致命问题另使整体直接判失败(ok=False), 与扣分叠加体现严重性。
# 类别扣分设上限 _CAT_MAX_PENALTY, 避免单一类别噪声把总分打满归零,
# 保证"致命=不过 / 数据瑕疵=扣分但可接受"的判定层次。
_SEV_PENALTY = {"critical": 10, "major": 5, "minor": 1, "info": 0}
_CAT_MAX_PENALTY = 45  # 单类别最多扣 45 分 -> 子分下限 55


def _score_category(weight, issues):
    """类别内 0-100 子分。按严重度扣分, 扣分封顶 _CAT_MAX_PENALTY。"""
    penalty = min(sum(_SEV_PENALTY[s] for s, _m in issues), _CAT_MAX_PENALTY)
    return max(0, 100 - penalty), penalty


# ═════════════════════════ 主检查 ═════════════════════════
def run_check(codes_dir=None, skip_build=False, threshold=None):
    """执行本体检查。codes_dir 缺省取本脚本所在目录。返回报告 dict。"""
    codes_dir = os.path.abspath(codes_dir or os.path.dirname(os.path.abspath(__file__)))
    repo_root = os.path.dirname(codes_dir)
    threshold = _DEFAULT_THRESHOLD if threshold is None else threshold

    # 入库(committed)文件集合 -> 保证本地==CI
    tracked = _tracked_files(repo_root)

    # A 链路
    chain = _check_chain(codes_dir, repo_root, skip_build)
    # 冒烟产物(若有)的悬空引用交给 C; 三元组交给 E
    smoke_triples = None
    smoke_dangling = []
    smoke_tables = []
    if chain["state"]["build_smoke"] and chain["state"]["build_smoke"]["rc"] == 0:
        tmp_out = os.path.join(codes_dir, "._ontology_check_smoke.nt")
        # 已删; 重跑一次快速构建供 C/E 复用(仍走真实构建, 无副作用)
        kb = chain["state"]["build_smoke"]["kb"]
        csvs = sorted(glob.glob(os.path.join(codes_dir, kb, "*.csv")))
        smoke_tables = [os.path.splitext(os.path.basename(c))[0] for c in csvs]
        try:
            cmd = [sys.executable, "multi_table.py", tmp_out] + csvs
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="ignore", cwd=codes_dir, timeout=120)
            if r.returncode == 0:
                _ok, smoke_triples, _bad = _parse_nt(tmp_out)
                smoke_dangling = _nt_dangling(smoke_triples or [])
        finally:
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass

    # B/C/D/E
    csv = _check_csv(codes_dir, tracked)
    nt = _check_nt(codes_dir, smoke_dangling, tracked)
    lexicon = _check_lexicon(codes_dir)
    consistency = _check_consistency(codes_dir, smoke_triples, smoke_tables)

    cats = {"chain": chain, "csv": csv, "nt": nt,
            "lexicon": lexicon, "consistency": consistency}

    # 汇总计分
    subtotals = {}
    total = 0.0
    for key, weight in WEIGHTS.items():
        subscore, penalty = _score_category(weight, cats[key]["issues"])
        subtotals[key] = {"weight": weight, "score": subscore, "penalty": penalty}
        total += weight * subscore / 100.0
    score = round(total, 1)

    critical = sum(1 for c in cats.values() for s, _m in c["issues"] if s == "critical")
    major = sum(1 for c in cats.values() for s, _m in c["issues"] if s == "major")
    minor = sum(1 for c in cats.values() for s, _m in c["issues"] if s == "minor")
    n_issues = critical + major + minor

    # 问题清单(按严重度排序)
    all_issues = []
    for key, c in cats.items():
        for sev, msg in c["issues"]:
            all_issues.append({"category": key, "severity": sev, "message": msg})
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    all_issues.sort(key=lambda i: order.get(i["severity"], 9))

    return {
        "ok": critical == 0 and score >= threshold,
        "score": score,
        "threshold": threshold,
        "n_issues": n_issues,
        "critical": critical, "major": major, "minor": minor,
        "categories": subtotals,
        "stats": {
            "csv_files": csv["stats"].get("files"),
            "nt_files": nt["stats"].get("files"),
            "lexicon_files": lexicon["stats"].get("files"),
            "kb_dirs": len(chain["links"]) and next(
                (l for l in chain["links"] if l["rel"] == "data_*"), {}).get("ok"),
        },
        "issues": all_issues,
        "verdict": ("通过" if critical == 0 and score >= _DEFAULT_THRESHOLD
                    else ("存在致命问题" if critical else "低于通过阈值")),
        "tool": "ontology_check", "version": VERSION,
    }


_DEFAULT_THRESHOLD = 60


# ═════════════════════════ CLI ═════════════════════════
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="工厂本体库本体检查(核查全库数据质量/链路断链/一致性, 纯标准库独立运行)")
    ap.add_argument("--dir", default=None, help="codes 目录(默认本脚本所在目录)")
    ap.add_argument("--threshold", type=int, default=_DEFAULT_THRESHOLD,
                    help=f"通过阈值(默认 {_DEFAULT_THRESHOLD})")
    ap.add_argument("--json", default=None, help="可选: 输出报告到 JSON 文件")
    ap.add_argument("--no-build", action="store_true", help="跳过真实构建冒烟测试")
    args = ap.parse_args(argv)

    rep = run_check(codes_dir=args.dir, skip_build=args.no_build,
                    threshold=args.threshold)

    # 打印报告
    w = 60
    print("=" * w)
    print("  工厂本体库 本体检查报告  ontology_check v%s" % VERSION)
    print("=" * w)
    print(f"  综合得分: {rep['score']} / 100    判定: {'✅ ' + rep['verdict'] if rep['ok'] else '❌ ' + rep['verdict']}")
    print(f"  通过阈值: {rep['threshold']}")
    print(f"  问题总数: {rep['n_issues']}  (致命 {rep['critical']} / 主要 {rep['major']} / 次要 {rep['minor']})")
    print("-" * w)
    print("  分类子分:")
    for key, meta in rep["categories"].items():
        print(f"    {key:<12}  {meta['score']:>5}/100  (权重 {meta['weight']}% 扣 {meta['penalty']})")
    print("-" * w)
    if rep["issues"]:
        print("  问题清单:")
        for it in rep["issues"]:
            print(f"    [{it['severity']:<8}] ({it['category']}) {it['message']}")
    else:
        print("  未发现任何问题 ✅")
    print("=" * w)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"报告已写入: {args.json}")

    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
