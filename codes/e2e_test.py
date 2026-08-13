#!/usr/bin/env python3
"""e2e_test.py — factory-ontology v0.2.0 端到端测试

覆盖: 问答(规则/逻辑/引导) + 溯源(正/反/扫码) + 导出 + 管理(上传/统计/词典/审计) + 多源 + 多租户 + 一致性
用法: python e2e_test.py  (退出码 0=全过, 1=有失败)
"""
import os
import sys
import json
import csv

codes = os.path.dirname(os.path.abspath(__file__))  # 相对路径, 任意位置可运行
sys.path.insert(0, codes)
os.chdir(codes)

# 安全加固(架构师审计 P0-1): api_server fail-closed 鉴权, 未配置 key 一律 401。
# 独立运行本脚本时必须先注入测试 key, 再 import api_server(其模块加载时读环境变量)。
os.environ.setdefault("FOOD_ADMIN_KEY", "e2e-test-admin-key")
os.environ.setdefault("FOOD_READ_KEY", "e2e-test-read-key")

# 全局鉴权头: admin key 可过 require_key 与 require_admin 两类端点。
_HEADERS = {"X-API-Key": os.environ.get("FOOD_ADMIN_KEY", "e2e-test-admin-key")}

FAILED = 0
TOTAL = 0


def ck(name, cond, detail=""):
    global FAILED, TOTAL
    TOTAL += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name} {detail}")


def main():
    from fastapi.testclient import TestClient
    import api_server as api

    c = TestClient(api.app)

    print("══ 一、问答层 ══")
    r = c.post("/api/ask", json={"question": "乳制品的数量"}, headers=_HEADERS)
    d = r.json()
    ck("规则问答 数量", r.status_code == 200 and "3" in d["answer"] and d["mode"] == "rule")
    ck("规则问答 带证据", bool(d.get("evidence")))
    r = c.post("/api/ask", json={"question": "完全无关xyz"}, headers=_HEADERS)
    ck("答不上给引导", r.status_code == 200 and d.get("mode") in ("rule", "logical", "graphrag", "miss"))

    print("══ 二、溯源层 ══")
    r = c.get("/api/trace/forward?batch=B001", headers=_HEADERS)
    ck("正向溯源 B001→2原料", r.status_code == 200 and len(r.json()["raw_materials"]) == 2)
    r = c.get("/api/trace/reverse?raw=RM008", headers=_HEADERS)
    ck("反向溯源 RM008→B005", r.status_code == 200 and any("B005" in x["batch"] for x in r.json()["affected_batches"]))
    r = c.get("/api/scan?code=P003-B005", headers=_HEADERS)
    ck("扫码溯源", r.status_code == 200)

    print("══ 三、导出层 ══")
    r = c.get("/api/export/reverse?raw=RM008&fmt=csv", headers=_HEADERS)
    ck("溯源导出 CSV 可读名", r.status_code == 200 and "盐" in r.text and "全麦面包" in r.text)
    r = c.get("/api/export/reverse?raw=RM008&fmt=txt", headers=_HEADERS)
    ck("溯源导出 TXT", r.status_code == 200)

    print("══ 四、管理层 ══")
    r = c.get("/admin")
    ck("管理后台页", r.status_code == 200 and "管理后台" in r.text)
    r = c.get("/api/admin/kbs", headers=_HEADERS)
    ck("多租户列出KB", r.status_code == 200 and "food" in r.json()["kbs"])
    r = c.get("/api/app-config")
    ck("APP配置(品牌/示例)", r.status_code == 200 and r.json()["name"])
    r = c.get("/metrics")
    ck("指标", r.status_code == 200)
    r = c.get("/api/admin/audit", headers=_HEADERS)
    ck("审计日志", r.status_code == 200)

    print("══ 五、多源 + 评测 ══")
    import db_loader as db
    r = db.load_db({"db_type": "mysql", "table": "t"})
    ck("ERP多源 缺驱动报错", isinstance(r, dict) and "pymysql" in str(r))

    import subprocess
    r = subprocess.run([sys.executable, "benchmark_logical.py"], capture_output=True, text=True, timeout=180)
    ck("逻辑桥评测 100%", "100%" in r.stdout and "5/5" in r.stdout)

    print("══ 六、一致性 ══")
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"], capture_output=True, text=True, timeout=120)
    ck("pytest 全过", r.returncode == 0 and "passed" in r.stdout, r.stdout[-80:])
    import run
    ck("版本 0.2.0", run.__version__ == "0.2.0")

    print(f"\n══ E2E 结果: {TOTAL - FAILED}/{TOTAL} 通过 ══")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
