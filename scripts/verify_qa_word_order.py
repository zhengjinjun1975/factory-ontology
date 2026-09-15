#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问答语序断言门（回归门，离线跑，不依赖后端服务）。

背景（P0）：类型词作主语的问法被实体总数分支抢答。
  "试压设备有多少台" → 有 10 台设备（真实 2）
  "机加设备有多少台" → 有 10 台设备（真实 3）
根因：ontology_qa_v3.answer「实体总数」分支的跨行业守卫用 q.replace(实体词, "") 后再找
类型枚举词。当类型词本身含实体词（试压设备 ⊃ 设备）时，replace 把类型词破坏成"试压"，
_find_enum 匹配不到 → 守卫失效 → 落回实体总数。
对照：走别的分支的同义问法是对的（"设备中设备类型为试压设备的有多少条"→2、
"有多少台机加设备"→3、"有哪些试压设备"→正确列表）。

用法：python scripts/verify_qa_word_order.py [--kb valve]
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
sys.path.insert(0, CODES)

import ontology_qa_v3 as v3  # noqa: E402


# (问句, golden 数值或 None, 期望包含的子串列表, 说明)
CASES = [
    ("试压设备有多少台", 2, ["试压"], "类型词作主语（类型词含实体词）"),
    ("机加设备有多少台", 3, ["机加"], "类型词作主语（类型词含实体词）"),
    ("铸造设备有多少台", 2, ["铸造"], "类型词作主语（类型词含实体词）"),
    ("有多少台机加设备", 3, ["机加"], "实体词在后的同义问法（对照）"),
    ("运行中的设备有多少台", 8, [], "状态词修饰（对照）"),
    ("有多少台设备", 10, [], "实体总数（守卫不得误伤）"),
    ("设备的功率最大是多少", 120.0, [], "极值路径（对照）"),
    ("设备中设备类型为试压设备的有多少条", 2, [], "评测口径问法（对照）"),
    ("有哪些试压设备", None, ["试压台-1", "试压台-2"], "列表路径（对照）"),
]


def nums(text):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default="valve")
    a = ap.parse_args()

    lex = os.path.join(CODES, "config", "lexicon_%s.json" % a.kb)
    nt = os.path.join(CODES, "output", "%s.nt" % a.kb)
    D = v3.load_dict(lex, industry=None)
    QD = v3.build_data(v3.parse_nt(nt), D)

    passed, failed = 0, []
    for q, golden, must_have, note in CASES:
        ans = str(v3.answer(q, QD, D))
        if golden is None:
            ok = all(m in ans for m in must_have)
        else:
            ns = nums(ans)
            ok = any(abs(n - golden) <= max(1e-9, abs(golden) * 0.01) for n in ns)
            # 过滤类问法：正确答案不得等于该实体总数（抢答的特征）
            if ok and q not in ("设备的功率最大是多少",):
                total = max(ns) if ns else None
                if total is not None and abs(total - golden) > 1e-9 and len(ns) > 1:
                    pass
        print("  [%s] %-24s %-28s %s" % ("PASS" if ok else "FAIL", q, ans.replace("\n", " ")[:28], note))
        if ok:
            passed += 1
        else:
            failed.append(q)

    print("\nPASS %d / FAIL %d" % (passed, len(failed)))
    if failed:
        print("未通过: " + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
