# -*- coding: utf-8 -*-
"""test_ontology_check.py — 本体检查(ontology_check.py)各核查类测试。

测试本体检查的 5 类核查能力(A 链路断链 / B CSV 质量 / C NT 本体 /
D 词典一致性 / E 本体一致性) + 计分 + CLI 退出码。

全部用例用临时目录构造合成数据, 不依赖真实仓库, 纯标准库、hermetic、快。
"""
import os
import sys
import json
import shutil
import tempfile

import pytest

# 让本体检查模块可导入(codes 目录)
_CODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODES_DIR not in sys.path:
    sys.path.insert(0, _CODES_DIR)

import ontology_check as oc  # noqa: E402


# ═══════════════ 工具: 构造临时 codes 目录 ═══════════════
def _mk_codes():
    """构造一个最小但结构完整的 codes 目录(含 chain 所需核心文件)。"""
    tmp = tempfile.mkdtemp(prefix="oc_test_")
    codes = os.path.join(tmp, "codes")
    os.makedirs(codes)
    os.makedirs(os.path.join(codes, "config"))
    os.makedirs(os.path.join(codes, "industrial_dict"))
    os.makedirs(os.path.join(codes, "data_valve"))
    os.makedirs(os.path.join(codes, "decision"))
    os.makedirs(os.path.join(tmp, "web", "server"))
    # chain 必需核心脚本(空壳即可, 不执行)
    for f in ["run.py", "csv_to_owl.py", "multi_table.py", "data_loader.py",
              "lexicon.py", "ontology_qa_v3.py", "graph_rag.py", "api_server.py"]:
        with open(os.path.join(codes, f), "w", encoding="utf-8") as fh:
            fh.write("")
    # 决策引擎(次要, 可缺省)
    with open(os.path.join(codes, "decision", "rules_engine.py"), "w", encoding="utf-8") as fh:
        fh.write("")
    # chain 必需: config/relations.json(对象属性配置, critical)
    with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh, ensure_ascii=False)
    # Web 桥接
    with open(os.path.join(tmp, "web", "server", "ontology.js"), "w", encoding="utf-8") as fh:
        fh.write("")
    return tmp, codes


def _cleanup(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════ A. 链路断链 ═══════════════
def test_chain_ok_on_complete_tree():
    tmp, codes = _mk_codes()
    try:
        # data_valve 至少一张 CSV, 否则 chain 判致命
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name\n1,阀\n")
        r = oc._check_chain(codes, os.path.dirname(codes), skip_build=True)
        sevs = {s for s, _m in r["issues"]}
        assert "critical" not in sevs, r["issues"]
        links = {l["rel"] for l in r["links"]}
        assert "web/server/ontology.js" in links
        assert "data_*" in links
    finally:
        _cleanup(tmp)


def test_chain_missing_web_bridge_is_critical():
    tmp, codes = _mk_codes()
    try:
        os.remove(os.path.join(tmp, "web", "server", "ontology.js"))
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name\n1,阀\n")
        r = oc._check_chain(codes, os.path.dirname(codes), skip_build=True)
        assert any(s == "critical" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


def test_chain_no_csv_is_critical():
    tmp, codes = _mk_codes()
    try:
        # 无任何 data_*/*.csv
        r = oc._check_chain(codes, os.path.dirname(codes), skip_build=True)
        assert any(s == "critical" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


# ═══════════════ B. CSV 数据质量 ═══════════════
def test_csv_good_file_no_issues():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "data_valve", "valve.csv")
        with open(p, "w", encoding="utf-8-sig") as fh:
            fh.write("id,name,type\n1,阀,gate\n2,蝶阀,butterfly\n")
        r = oc._check_csv(codes)
        assert r["stats"]["files"] == 1
        assert r["stats"].get("unreadable", 0) == 0
        assert not any(s == "major" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


def test_csv_dup_pk_detected():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "data_valve", "valve.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("id,name\n1,阀\n1,阀\n2,蝶阀\n")
        r = oc._check_csv(codes)
        assert r["stats"]["duppk"] == 1, r["stats"]
        assert any("重复主键" in m for s, m in r["issues"] if s == "major")
    finally:
        _cleanup(tmp)


def test_csv_empty_pk_detected():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "data_valve", "valve.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("id,name\n,阀\n2,蝶阀\n")
        r = oc._check_csv(codes)
        assert r["stats"]["emptypk"] == 1, r["stats"]
    finally:
        _cleanup(tmp)


def test_csv_empty_table_detected():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "data_valve", "valve.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("id,name\n")
        r = oc._check_csv(codes)
        assert r["stats"]["empty"] == 1, r["stats"]
        assert any(s == "critical" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


# ═══════════════ C. NT 本体质量 ═══════════════
def test_nt_good_no_dangling():
    tmp, codes = _mk_codes()
    try:
        out = os.path.join(codes, "output")
        os.makedirs(out)
        p = os.path.join(out, "valve.nt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(
                f"<{oc.NS}Valve> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                f"<http://www.w3.org/2002/07/owl#Class> .\n"
                f"<{oc.NS}valve_1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{oc.NS}Valve> .\n"
                f"<{oc.NS}valve_1> <{oc.NS}name> \"阀门\" .\n"
            )
        r = oc._check_nt(codes, [], tracked=None)
        assert r["stats"]["files"] == 1
        assert r["stats"].get("dangling_refs", 0) == 0
        assert not any(s == "major" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


def test_nt_dangling_detected():
    tmp, codes = _mk_codes()
    try:
        out = os.path.join(codes, "output")
        os.makedirs(out)
        p = os.path.join(out, "valve.nt")
        with open(p, "w", encoding="utf-8") as fh:
            # 对象引用 NS+valve_999 但 subject 未出现 -> 悬空
            fh.write(f"<{oc.NS}valve_1> <{oc.NS}part_of> <{oc.NS}valve_999> .\n")
        r = oc._check_nt(codes, [], tracked=None)
        assert r["stats"]["dangling_refs"] == 1, r["stats"]
        assert any("悬空" in m for s, m in r["issues"])
    finally:
        _cleanup(tmp)


def test_nt_empty_ont_detected():
    tmp, codes = _mk_codes()
    try:
        out = os.path.join(codes, "output")
        os.makedirs(out)
        p = os.path.join(out, "empty.nt")
        open(p, "w", encoding="utf-8").close()
        r = oc._check_nt(codes, [], tracked=None)
        assert r["stats"]["empty"] == 1, r["stats"]
        assert any(s == "major" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


# ═══════════════ D. 词典一致性 ═══════════════
def test_lexicon_good():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "industrial_dict", "00_basis.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({
                "description": "基础", "version": "1.0", "industry": "general",
                "type_cn2en": {"泵": "pump", "阀": "valve"},
            }, fh, ensure_ascii=False)
        r = oc._check_lexicon(codes)
        assert r["stats"]["files"] == 1
        assert r["stats"].get("invalid", 0) == 0
        assert not any(s == "major" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


def test_lexicon_missing_required_key():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "industrial_dict", "00_basis.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"type_cn2en": {"泵": "pump"}}, fh, ensure_ascii=False)
        r = oc._check_lexicon(codes)
        assert r["stats"]["missingkey"] == 1, r["stats"]
        assert any(s == "major" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


def test_lexicon_invalid_json():
    tmp, codes = _mk_codes()
    try:
        p = os.path.join(codes, "industrial_dict", "00_basis.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json ")
        r = oc._check_lexicon(codes)
        assert r["stats"]["invalid"] == 1, r["stats"]
        assert any(s == "critical" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


# ═══════════════ E. 本体一致性 ═══════════════
def test_consistency_missing_relations_is_critical():
    tmp, codes = _mk_codes()
    try:
        # 移除 _mk_codes 默认创建的 relations.json -> 判致命
        os.remove(os.path.join(codes, "config", "relations.json"))
        r = oc._check_consistency(codes, smoke_triples=[], smoke_tables=[])
        assert any(s == "critical" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


def test_consistency_target_class_missing():
    tmp, codes = _mk_codes()
    try:
        rel = {
            "valve": {
                "object_properties": {
                    "material": {"target_class": "Material"},
                }
            }
        }
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump(rel, fh, ensure_ascii=False)
        # 冒烟产物里表 valve 被建模但 Material 类未声明 -> missing_class
        smoke_triples = [
            (f"{oc.NS}valve_1", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>",
             f"<{oc.NS}ValveClass>")
        ]
        r = oc._check_consistency(codes, smoke_triples, smoke_tables=["valve"])
        assert r["stats"]["missing_class"] == 1, r["stats"]
        assert any(s == "major" for s, _m in r["issues"])
    finally:
        _cleanup(tmp)


def test_consistency_ok_when_class_declared():
    tmp, codes = _mk_codes()
    try:
        rel = {"valve": {"object_properties": {"material": {"target_class": "Material"}}}}
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump(rel, fh, ensure_ascii=False)
        smoke_triples = [
            (f"{oc.NS}Material",
             "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>",
             "<http://www.w3.org/2002/07/owl#Class>"),
            (f"{oc.NS}valve_1", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>",
             f"<{oc.NS}ValveClass>"),
            (f"{oc.NS}valve_1", f"<{oc.NS}material>", f"<{oc.NS}Material>"),
        ]
        r = oc._check_consistency(codes, smoke_triples, smoke_tables=["valve"])
        assert r["stats"].get("missing_class", 0) == 0, r["stats"]
        assert r["stats"]["class_ok"] == 1, r["stats"]
        assert not any(s == "major" for s, _m in r["issues"]), r["issues"]
    finally:
        _cleanup(tmp)


# ═══════════════ 计分 / 阈值 / CLI ═══════════════
def test_score_category_penalty():
    # 无问题 -> 100 分
    assert oc._score_category(20, []) == (100, 0)
    # 一条 major(-5) -> 95
    assert oc._score_category(20, [("major", "x")]) == (95, 5)
    # 致命问题扣 10
    assert oc._score_category(20, [("critical", "x")]) == (90, 10)


def test_run_check_returns_report_and_ok():
    tmp, codes = _mk_codes()
    try:
        # 让仓库可通过: 给 data_valve 一张好 CSV, relations.json, 词典
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name,type\n1,阀,gate\n2,蝶阀,butterfly\n")
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump({"valve": {"object_properties": {}}}, fh, ensure_ascii=False)
        with open(os.path.join(codes, "industrial_dict", "00_basis.json"), "w", encoding="utf-8") as fh:
            json.dump({"description": "基础", "version": "1.0", "industry": "general",
                       "type_cn2en": {"泵": "pump"}}, fh, ensure_ascii=False)
        r = oc.run_check(codes_dir=codes, skip_build=True, threshold=60)
        assert isinstance(r, dict)
        assert r["ok"] is True, r
        assert r["score"] >= 60
        assert set(r["categories"].keys()) == {"chain", "csv", "nt", "lexicon", "consistency"}
        assert r["critical"] == 0
    finally:
        _cleanup(tmp)


def test_threshold_high_fails():
    tmp, codes = _mk_codes()
    try:
        # 同上构造, 但阈值设为 200(不可能达) -> ok=False
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name,type\n1,阀,gate\n")
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump({}, fh, ensure_ascii=False)
        r = oc.run_check(codes_dir=codes, skip_build=True, threshold=200)
        assert r["ok"] is False
        assert r["score"] < 200
    finally:
        _cleanup(tmp)


def test_cli_exit_code_zero_on_pass():
    tmp, codes = _mk_codes()
    try:
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name,type\n1,阀,gate\n")
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump({}, fh, ensure_ascii=False)
        rc = oc.main(["--dir", codes, "--no-build", "--threshold", "1"])
        assert rc == 0
    finally:
        _cleanup(tmp)


def test_cli_json_output_file():
    tmp, codes = _mk_codes()
    try:
        with open(os.path.join(codes, "data_valve", "valve.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name,type\n1,阀,gate\n")
        with open(os.path.join(codes, "config", "relations.json"), "w", encoding="utf-8") as fh:
            json.dump({}, fh, ensure_ascii=False)
        rep_path = os.path.join(tmp, "report.json")
        rc = oc.main(["--dir", codes, "--no-build", "--json", rep_path])
        assert rc == 0
        assert os.path.isfile(rep_path)
        with open(rep_path, encoding="utf-8") as fh:
            rep = json.load(fh)
        assert rep["tool"] == "ontology_check"
    finally:
        _cleanup(tmp)
