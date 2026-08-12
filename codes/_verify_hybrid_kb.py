#!/usr/bin/env python3
# 实测脚本：本体中心、知识库配合缺陷修复验证
import json, sys, urllib.request

BASE = "http://127.0.0.1:8010"
H = {"X-API-Key": "test-admin-key", "Content-Type": "application/json"}

def call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def show(tag, r):
    m = r.get("mode", "?")
    ans = (r.get("answer") or "")[:120].replace("\n", " ")
    ev = r.get("evidence") or []
    print(f"[{tag}] mode={m}")
    print(f"   answer: {ans}")
    if m == "kb_rag":
        print(f"   evidence[{len(ev)}]: " + json.dumps(ev[:2], ensure_ascii=False)[:300])
    if m == "hybrid":
        print(f"   hits[{len(r.get('hits') or [])}]: " + json.dumps([h.get('entity') for h in (r.get('hits') or [])], ensure_ascii=False))
    print()

if __name__ == "__main__":
    import requests as _req
    print("=== 1) valve kb: 上传文档《真空度维护规范》 ===")
    resp = _req.post(BASE + "/api/knowledge/ingest",
                     headers={"X-API-Key": "test-admin-key"},
                     files={"file": ("vacuum.txt", "真空度维护规范\n真空度应保持5Pa以下。每天开机前需检查真空泵运行状态，确保真空度达到规定要求后再进行加工。", "text/plain")},
                     data={"kb": "valve", "doc_id": "vacuum_spec"}, timeout=120)
    print("   ingest:", json.dumps(resp.json(), ensure_ascii=False))

    print("=== 2) valve kb: 问《真空度的维护要求》 → 期望 kb_rag ===")
    show("ask-真空度", call("POST", "/api/ask", {"question": "真空度的维护要求", "kb": "valve"}))

    print("=== 3) valve kb: 问《有多少台设备》(本体rule真答) → 期望 rule 不退化 ===")
    show("ask-设备数", call("POST", "/api/ask", {"question": "有多少台设备", "kb": "valve"}))

    print("=== 4) food kb: 无文档, 问《乳制品的数量》→ 期望 rule ===")
    show("ask-food-rule", call("POST", "/api/ask", {"question": "乳制品的数量", "kb": "food"}))

    print("=== 5) food kb: 无文档, 问模糊词触发hybrid → 期望 hybrid/miss 不报错 ===")
    show("ask-food-hybrid", call("POST", "/api/ask", {"question": "神秘的乳品风味描述", "kb": "food"}))
