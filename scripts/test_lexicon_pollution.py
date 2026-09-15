"""lexicon_agent 三处污染修复的自检(assert 式, 无框架)。

验证:
  ① _valid_cn  含拉丁字母判为无效
  ② _infer_cn_from_name  未命中英文词丢弃, 不产生 "machinefailure" 这类 key, 不产生重复字
  ③ _build_attr_mapping  LLM 回英文字段名时逐级回落; 推断不出则跳过(不写脏 key)
  ④ _rule_enum_mapping   英文枚举值不产出 值=>值 恒等映射
"""
import os
import re
import sys

sys.path.insert(0, r"D:\factory-ontology\codes")
from agents.lexicon_agent import LexiconAgent

a = LexiconAgent()
fails = []


def chk(name, cond, detail=""):
    if not cond:
        fails.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")


print("① _valid_cn 判据")
chk("'udi' 无效", not a._valid_cn("udi"))
chk("'product编号' 无效", not a._valid_cn("product编号"))
chk("'编号' 有效", a._valid_cn("编号"))
chk("空串无效", not a._valid_cn(""))

print("\n② _infer_cn_from_name 不产英文/重复字")
for f in ("machinefailure", "Rotational_speed_", "product_id", "quality状态"):
    cn = a._infer_cn_from_name(f)
    half = len(cn) // 2
    dup = len(cn) >= 4 and len(cn) % 2 == 0 and cn[:half] == cn[half:]
    ok = not re.search(r"[A-Za-z]", cn) and not dup
    chk(f"{f} -> {cn!r}", ok, "" if ok else "(仍含拉丁字母或重复字)")

print("\n③ _build_attr_mapping 拦截英文 key")
fi = {
    "udi": {"is_numeric": True, "values": [1, 2], "num_values": 2},
    "product_id": {"is_numeric": True, "values": [1], "num_values": 1},
    "口径": {"is_numeric": True, "values": [1], "num_values": 1},
}
am = {"udi": {"cn": "udi"}, "product_id": {"cn": "product编号"}, "口径": {"cn": "口径"}}
cn2en, en2cn = a._build_attr_mapping(fi, am)
bad = [k for k in cn2en if re.search(r"[A-Za-z]", k)]
chk("attr_cn2en 无含拉丁字母的 key", not bad, f"残留 {bad}" if bad else "干净")

print("\n④ _rule_enum_mapping 无 值=>值 恒等映射")
fi2 = {"status": {"is_numeric": False, "values": ["running", "idle"], "num_values": 2},
       "材质": {"is_numeric": False, "values": ["焊接设备", "铸造设备"], "num_values": 2}}
em = a._rule_enum_mapping(fi2)
identity = [(f, v) for f, m in em.items() for v, cn in m.items() if v == cn and re.search(r"[A-Za-z]", v)]
chk("英文枚举无恒等映射", not identity, f"残留 {identity}" if identity else "干净")
chk("中文枚举仍保留", em.get("材质", {}).get("焊接设备") == "焊接设备", str(em.get("材质")))

print(f"\n断言: {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + str(fails)}")
sys.exit(1 if fails else 0)
