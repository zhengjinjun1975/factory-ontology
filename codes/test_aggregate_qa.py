#!/usr/bin/env python3
"""test_aggregate_qa.py — 聚合/分组查询回归测试(规则引擎, 确定性)。

覆盖三类新功能:
  1. '各X的Y' 分组计数: 各车间设备总数 / 各类型产品数
  2. 'XX分布' 枚举分布: 状态分布 / 类型分布 / 设备类型分布
  3. '平均/合计' 数值聚合: 平均功率 / 总库存 (回归, 含实体作用域)

用法: python test_aggregate_qa.py <nt> <lexicon.json>  (默认 valve)
"""

import sys
import os
import json
import ontology_qa_v3 as v3


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    nt = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base, "output", "valve.nt")
    lex = sys.argv[2] if len(sys.argv) > 2 else os.path.join(base, "config", "lexicon_valve.json")
    D = json.load(open(lex, encoding="utf-8"))
    data = v3.build_data(v3.parse_nt(nt), D)

    cases = [
        # (问题, 期望包含的子串, 期望不包含的子串)
        ("各车间设备总数", "机加车间", "总数为10"),
        ("各车间的设备数", "检测车间", None),
        ("各类型产品数", "球阀", None),
        ("各状态设备数", "运行中", None),
        ("设备状态分布", "运行中", None),
        ("状态分布", "已入库", None),  # 裸分布自动选该字段覆盖最广的实体类(此处为批次)
        ("设备类型分布", "机加设备", None),
        ("类型分布", "机加设备", None),
        ("车间分布", "机加车间", None),
        ("平均功率", "平均值", None),
        ("设备平均功率", "平均值", None),
        ("总库存", "总和", None),
        ("客户总数", "客户总数", None),
        ("有多少台设备", "台设备", None),
        ("运行中的设备有多少", "台", None),
    ]
    failed = 0
    for q, must_have, must_not in cases:
        ans = v3.answer(q, data, D)
        ok = (must_have in ans) if must_have else True
        if must_not:
            ok = ok and (must_not not in ans)
        if ok:
            print("PASS  %-22s -> %s" % (q, ans.replace("\n", " / ")[:90]))
        else:
            failed += 1
            print("FAIL  %-22s -> %s" % (q, ans.replace("\n", " / ")[:120]))
    print("\n%d/%d 通过" % (len(cases) - failed, len(cases)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
