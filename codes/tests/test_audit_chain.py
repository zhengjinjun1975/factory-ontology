#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_chain 自检: 验证哈希链核心不变量(防篡改/防删行/序号连续/导出).
lazy rule: 非平凡逻辑留一个可跑检查. 跑: python tests/test_audit_chain.py
"""
import os
import sys
import json
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit_chain import AuditChain, AuditChainError

fails = []
def ck(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not cond:
        fails.append(name)

tmp = tempfile.mkdtemp(prefix="audit_chain_test_")
db = os.path.join(tmp, "test_audit.db")

# 1. 记录决策 + 溯源 + 备注
c = AuditChain(db)
r1 = c.record_decision(scenario="阀门选型", reasoning="口径大/对夹安装/价格2560在预算",
                       outcome="选择D371X蝶阀", category="procurement",
                       entities=["Valve_D371X", "RM003"], confidence=0.9)
r2 = c.record_trace(source="RM008", relation="belongsToBatch", target="Food_batches_B005")
r3 = c.record_decision(scenario="维护优先级", reasoning="温度302超阈值/磨损205",
                       outcome="紧急检修", category="maintenance", confidence=0.8)
r4 = c.record_note("审计留痕: 厂区A阀门批次更换")

ck("四条记录 id 连续递增(1-4)", [r1, r2, r3, r4] == [1, 2, 3, 4], f"{[r1,r2,r3,r4]}")

# 2. 校验链完整
ok, bad = c.verify_chain()
ck("空改前链完整 PASS", ok, f"bad={bad}")
ck("决策计数=2", c.audit_report()["decision_records"] == 2)

# 3. decisions 过滤 category
decs = c.decisions(category="procurement")
ck("decisions 过滤 procurement 得1条", len(decs) == 1 and decs[0]["scenario"] == "阀门选型", f"{len(decs)}")

# 4. 篡改检测: 改某条 payload, verify 应 FAIL 且标记 checksum_mismatch
with sqlite3.connect(db) as conn:
    cur = conn.execute("SELECT id, payload FROM ledger WHERE id=2")
    rid, pl = cur.fetchone()
    altered = pl.replace("Food_batches_B005", "Food_batches_B999")  # 篡改 target
    conn.execute("UPDATE ledger SET payload=? WHERE id=?", (altered, rid))
ok2, bad2 = c.verify_chain()
ck("篡改 payload 后链 FAIL", not ok2)
ck("篡改被标记 checksum_mismatch",
   any(b[1] == "checksum_mismatch" for b in bad2), f"{[b[1] for b in bad2]}")

# 5. 删除检测: 删中间行(如 id=1), verify 应抓序号缝隙
c2 = AuditChain(os.path.join(tmp, "del_test.db"))
c2.record_decision(scenario="A", reasoning="r1", outcome="o1")
c2.record_decision(scenario="B", reasoning="r2", outcome="o2")
c2.record_decision(scenario="C", reasoning="r3", outcome="o3")
with sqlite3.connect(c2.db_path) as conn:
    conn.execute("DELETE FROM ledger WHERE id=2")  # 硬删中间行
ok3, bad3 = c2.verify_chain()
ck("删行后链 FAIL", not ok3)
ck("删行被标记 gap(序号缝隙)",
   any(b[1] == "gap" for b in bad3), f"{[b[1] for b in bad3]}")

# 6. 导出 json/csv/prov-o
c3 = AuditChain(os.path.join(tmp, "exp_test.db"))
c3.record_decision(scenario="X", reasoning="r", outcome="o", entities=["E1"])
c3.record_trace(source="RM", target="B")
for fmt, ext in [("json", "json"), ("csv", "csv"), ("prov-o", "prov-o.json")]:
    op = os.path.join(tmp, f"out.{ext}")
    r = c3.export_audit(op)
    ck(f"导出 {fmt} → {r['n']}条 verified={r['verified']}", os.path.exists(op) and r["verified"], op)
    if fmt == "prov-o":
        d = json.load(open(op, encoding="utf-8"))
        ck("prov-o 含 wasDerivedFrom 链", len(d.get("wasDerivedFrom", [])) >= 1,
           f"derived={len(d.get('wasDerivedFrom',[]))}")

# 清理
import shutil
shutil.rmtree(tmp, ignore_errors=True)

print("---")
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS (audit_chain 哈希链/决策记录/防篡改/防删/导出 自检通过)")
