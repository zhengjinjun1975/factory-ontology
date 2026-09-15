# -*- coding: utf-8 -*-
"""工厂本体问答回归门测试。纯标准库 + fastapi。运行: python test_qa_gate.py"""
import os
import sys

sys.path.insert(0, "D:/factory-ontology/codes")

import ontology_qa_v3
import evidence_norm
import fusion
import query_understand

ROOT = "D:/factory-ontology"


def load(nt_name, lex_name):
    triples = ontology_qa_v3.parse_nt(ROOT + "/codes/output/" + nt_name)
    D = ontology_qa_v3.load_dict(ROOT + "/codes/config/" + lex_name)
    data = ontology_qa_v3.build_data(triples, D)
    return data, D


def group1():
    vdata, vD = load("valve.nt", "lexicon_valve.json")
    r = ontology_qa_v3.answer("一共有多少个阀门", vdata, vD)
    assert "无相关数据" in r, "库外概念应拒答总数: %r" % r
    r = ontology_qa_v3.answer("一共有多少条记录", vdata, vD)
    assert "条记录" in r, "元查询应豁免: %r" % r

    fdata, fD = load("food.nt", "lexicon_food.json")
    r = ontology_qa_v3.answer("类型分布", fdata, fD)
    assert "设备类型有" in r and "产品类型有" in r, "类型分布应按实体类分列: %r" % r
    r = ontology_qa_v3.answer("保质期最长的产品", fdata, fD)
    assert len([x for x in r.split("、") if x.strip()]) >= 2, "并列极值应全列: %r" % r


def group2():
    ev = evidence_norm.normalize_evidence({'entity': 'X', 'source': 'rule'})
    assert isinstance(ev, list) and len(ev) >= 1, "normalize_evidence: %r" % (ev,)

    fu = fusion.fuse('q', [{'answer': 'A', 'source': 'rule', 'score': 1.0,
                            'evidence': [{'entity': 'E', 'source': 'rule'}],
                            'structured': None}])
    assert isinstance(fu, dict), "fuse 应返回 dict: %r" % (fu,)
    assert fu.get('confidence') == 'high', "confidence 应为 high: %r" % (fu,)
    assert fu.get('ok'), "ok 应为真: %r" % (fu,)

    qu = query_understand.understand('产品类型有哪些', {})
    assert isinstance(qu, dict), "understand 应返回 dict: %r" % (qu,)
    for k in ('intent', 'entities', 'resolved', 'is_cross_domain'):
        assert k in qu, "understand 缺键 %s: %r" % (k, qu)


def group3():
    os.environ.setdefault("FOOD_READ_KEY", "test-read-key")
    import api_server
    from fastapi.testclient import TestClient

    client = TestClient(api_server.app)
    headers = {"X-API-Key": "test-read-key"}
    required = ["ok", "mode", "answer", "evidence", "engines", "structured", "no_basis", "kb"]
    allowed_conf = {"high", "medium", "low", "none"}

    for q in ("产品类型有哪些", "一共有多少条记录", "一共有多少个阀门", "保质期最长的产品"):
        resp = client.post("/api/ask", json={"question": q, "kb": "food"}, headers=headers)
        assert resp.status_code == 200, "HTTP %s for %r" % (resp.status_code, q)
        body = resp.json()
        assert isinstance(body, dict), "响应应 dict: %r" % (body,)
        for f in required:
            assert f in body, "缺字段 %s (q=%r): %r" % (f, q, body)
        if "confidence" in body:
            assert body["confidence"] in allowed_conf, \
                "confidence 非法 (q=%r): %r" % (q, body["confidence"])


def main():
    group1()
    group2()
    group3()
    print("PASS")


if __name__ == "__main__":
    main()