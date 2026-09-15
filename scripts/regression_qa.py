"""问答回归套件（ad-hoc 断言，非 pytest suite）。

每次改动 ontology_qa_v3.py / api_server.py / ask_service.py 后跑一遍，
防止"修一处、坏一处"——本轮已发生 4 次（filter 主键回落、驼峰末词、
id 后缀误命中、status 分支抢答），全靠全量回归才发现。

用法:
    python scripts/regression_qa.py                     # 默认 127.0.0.1:8000
    FACTORY_API_BASE=http://127.0.0.1:8003 python scripts/regression_qa.py

前置: 后端在跑（codes/api_server.py），读键默认 test-read-key。
每一条断言对应一个真实踩过的坑，根因见
        D:\\knowledge-base\\obsidian-vault\\projects\\工厂本体建模-过程变化日志-2026-09-15.md
"""
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
BASE = os.environ.get("FACTORY_API_BASE") or "http://127.0.0.1:8000"
KEY = os.environ.get("FACTORY_READ_KEY") or "test-read-key"
sys.path.insert(0, CODES)

R = []


def chk(name, cond, detail=""):
    R.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


def ask_http(q, kb="valve"):
    """走 HTTP：覆盖润色 + 出口净化（两条只在服务层生效）。"""
    req = urllib.request.Request(
        f"{BASE}/api/ask",
        data=json.dumps({"question": q, "kb": kb}, ensure_ascii=False).encode("utf-8"),
        headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=90).read().decode("utf-8"))
    return str((d.get("data") or d).get("answer") or d)


import api_server as A          # noqa: E402
import ontology_qa_v3 as v3     # noqa: E402


def ans(q, kb="valve"):
    """直接调引擎。_get_kb_ctx 在异常切库顺序下会返回 None，先激活默认库再取。"""
    c = None
    for _ in range(2):
        try:
            c = A._get_kb_ctx(kb)
        except Exception:
            c = None
        if c:
            break
        A._get_kb_ctx("valve")
    if not c:
        return f"[取库失败: {kb}]"
    return str(v3.answer(q, c["QDATA"], c["D"]))


print("① filter：属性等值过滤计数")
for q, g in [("批次配料中原料编号为R007的有多少条", 8),
             ("客户中信用等级为A的有多少条", 4),
             ("质检中检查项目为上密封试验的有多少条", 9),
             ("设备中车间为机加车间的有多少条", 3),
             # 实体消歧：问句点名的表与属性所属表不一致，按问句实体名重试
             ("批次中产品编号为P007的有多少条", 5)]:
    a = ans(q)
    chk(f"filter: {q[:26]}", f"{g}" in a, a[:50])

print("①b filter：主键过滤（编号为X）—— 同长词/主键回落相关")
for kb, q, g in [("chem", "原料中编号为M05的有多少条", 1),
                 ("auto_parts", "客户中编号为C004的有多少条", 1),
                 # 带表前缀的 snake 键名（orders_customer_id）必须能取到值
                 ("auto_parts", "订单中客户编号为C003的有多少条", 6)]:
    a = ans(q, kb)
    chk(f"主键[{kb}]: {q[:18]}", f"有 {g} 条" in a, a[:52])

print("①c filter 优先级：问句点名了别的属性时 status 分支必须让路（反之不能坏）")
for kb, q, w in [("valve", "原料中质检结果为合格的有多少条", "有 9"),
                 ("food", "质检中结果为合格的有多少条", "有 9")]:
    a = ans(q, kb)
    chk(f"filter优先[{kb}]: {q[:12]}", w in a, a[:50])
for q, w in [("运行中的设备有多少台", "有 8"), ("待机状态的设备有多少台", "有 1")]:
    a = ans(q)
    chk(f"status分支: {q[:12]}", w in a, a[:50])

print("①d enum：关系表外键枚举（值应是 ID，不是实例名/显示名）")
for kb, q, want in [("valve", "批次配料的原料编号有哪些", ["R001", "R010"]),
                    ("valve", "批次的产品编号有哪些", ["P003", "P008"]),
                    ("valve", "销售的产品编号有哪些", ["P001", "P008"]),
                    ("food", "批次配料的批次编号有哪些", ["B001", "B009"]),
                    ("chem", "批次的编号有哪些", ["B001", "B005"]),
                    ("auto_parts", "客户的编号有哪些", ["C001", "C004"])]:
    a = ans(q, kb)
    residue = [x for x in ("Customers_", "Valve_", "Food_", "Chem_", "Products_") if x in a]
    chk(f"enum[{kb}]: {q[:18]}", all(w in a for w in want) and not residue,
        a[:50] + (f" | 残留{residue}" if residue else ""))

print("①e 词典：属性中文名解析（实体名不得被当成属性名）")
for q, w in [("原料中库存最大是多少", "5000"),      # 实体名"原料"抢赢真属性"库存"
             ("原料的库存总计是多少", "16000")]:
    a = ans(q)
    chk(f"极值/聚合: {q[:14]}", w in a, a[:52])
a = ans("批次的存储条件有哪些", "food")             # 属性长在另一张表（属性反查）
chk("属性反查[food]: 存储条件", ("冷藏" in a or "常温" in a) and "Food_batches_" not in a, a[:52])

print("①f 润色保真：不得改写事实（数字独立比对 + 单位原样保留）")
_js = open(os.path.join(CODES, "ask_service.py"), encoding="utf-8").read()
chk("数字按独立数字比对(有 \\d 边界)", "(?<!\\d)" in _js and "(?!\\d)" in _js)
chk("单位原样保留", "_units" in _js and "_unit_ok" in _js)
chk("汉英间不加空格", "_gap" in _js)
a = ask_http("质检的压力规则有哪些")
chk("单位 MPa 未被改成中文", "MPa" in a, a[:56])

print("①g 出口净化：任何引擎的模型独白都不得进入用户答案")
_leaky = ask_http("原料中库存最大是多少")
_bad = [w for w in ("先确认问题", "先问一句", "先数", "图上", "图里", "子图",
                    "decimal", "知识图谱", "没有给任何") if w in _leaky]
chk("无依据题不输出模型独白", not _bad, f"命中内部词{_bad} | {_leaky[:46]}")
_norm = ask_http("设备有多少台")
chk("正常题未被误伤", "10" in _norm, _norm[:46])

print("② 回归：以上改动不得影响其它意图")
for q, e in [("设备有多少台", "10"), ("设备的设备类型有哪些", "机加设备"),
             ("设备的车间有哪些", "机加车间"), ("设备的功率最大是多少", "120"),
             ("设备的温度平均是多少", "温度平均"), ("运行中的设备有多少台", "8"),
             ("批次有多少条", "15"), ("产品的压力等级有哪些", "PN10"),
             ("客户的信用等级有哪些", "A")]:
    a = ans(q)
    chk(f"回归: {q[:22]}", e in a, a[:46])

print("③ 端口口径统一（默认 8000，env 可覆盖）")
for f, tag in [(os.path.join(ROOT, "scripts", "eval_hit_rate.py"), "eval_hit_rate"),
               (os.path.join(ROOT, "scripts", "eval_ablation.py"), "eval_ablation")]:
    t = open(f, encoding="utf-8").read()
    chk(f"{tag}: 默认 8000", 'or "http://127.0.0.1:8000"' in t)
    chk(f"{tag}: 无残留 8003 默认", 'default="http://127.0.0.1:8003"' not in t)
    chk(f"{tag}: 支持 FACTORY_API_BASE", "FACTORY_API_BASE" in t)

print("④ BFF 报错中文化")
_jsf = os.path.join(ROOT, "web", "server", "ontology.js")
js = open(_jsf, encoding="utf-8").read()
chk("含中文可操作提示", "无法连接后端" in js and "api_server.py" in js)
chk("超时也中文化", "后端超时" in js and "秒无响应" in js)
chk("netErr 统一函数已建", "function netErr(" in js)
_bare = [l for l in js.splitlines() if "String(e.message || e)" in l and not l.strip().startswith("//")]
chk("代码中无裸传原生 message", not _bare, str(_bare[:2]))
chk("调用点已改 netErr", js.count("error: netErr(e)") >= 20, f"{js.count('error: netErr(e)')} 处")
r = subprocess.run(["node", "--check", _jsf], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
chk("ontology.js 语法通过", r.returncode == 0, (r.stderr or "")[:90])

print("⑤ 端到端：后端真在跑且能答")
try:
    h = urllib.request.urlopen(f"{BASE}/health", timeout=8).read().decode()
    chk("/health ok", '"status":"ok"' in h)
    a = ask_http("设备有多少台")
    chk("问答通", "10" in a, a[:46])
except Exception as e:
    chk("后端可达", False, str(e)[:80])

np_, nf = sum(1 for _, o, _ in R if o), sum(1 for _, o, _ in R if not o)
print(f"\n断言: {np_}/{len(R)} PASS, {nf} FAIL")
for n, o, d in R:
    if not o:
        print(f"  ✗ {n}   {d}")
sys.exit(1 if nf else 0)
