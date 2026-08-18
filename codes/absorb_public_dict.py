#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""absorb_public_dict.py — 公共工业本体词典「服务式吸收」机制。

原理（用户定位：公共词典 = 1/3兜底 + 1/3行业跨界 + 1/3本专业边缘）：
  * 公共词典不是一次性人工建好，而是「服务企业 → 构建本地KB → 吸收公共概念 → 补入公共词典 → 供下一个企业用」滚雪球式增长。
  * 本厂建模往往发现不了真正的公共知识（单厂只见自己的特殊词），跨行业统计才能暴露「哪些概念是多个企业/行业共有的」→ 这些就是公共层该收的。

用法：
  # 扫描全部 KB 词典, 提炼跨行业公共概念, 合并进公共词典
  python absorb_public_dict.py --scan
  # 服务完一个企业后, 增量吸收该企业词典的公共概念
  python absorb_public_dict.py --kb config/lexicon_xxx.json
  # 看当前公共层统计
  python absorb_public_dict.py --stats
"""
import os
import sys
import json
import glob
from collections import Counter

# 本模块目录 = codes/
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
PUBLIC_DIR = os.path.join(ROOT, "industrial_dict")
PUBLIC_MAIN = os.path.join(PUBLIC_DIR, "device_types.json")

# 公共词典合并键位（只吸收这几类；attr/numeric 属工厂字段，不吸收入公共层）
_MERGE_KEYS = ("type_cn2en", "status_cn2en", "synonym_map", "entity_cn2en")
# 吸收判定阈值: 概念出现在 ≥N 个 KB 词典才视为「公共概念」候选
CROSS_KB_THRESHOLD = 3


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_public():
    d = load_json(PUBLIC_MAIN)
    if not d:
        d = {
            "description": "公共工业本体词典（服务式吸收生成）",
            "version": "0.3.0",
            "built": "2026-08-18",
            "type_cn2en": {}, "status_cn2en": {}, "synonym_map": {},
            "entity_cn2en": {}, "fault_cn2en": {}, "product_type_cn2en": {},
        }
    return d


def scan_kb_lexicons():
    """扫描 config/ 下所有 lexicon_*.json, 返回 {概念: 出现KB数} 统计。"""
    counter = Counter()
    lex_files = glob.glob(os.path.join(CONFIG_DIR, "lexicon_*.json"))
    for lp in lex_files:
        d = load_json(lp)
        if not d:
            continue
        for key in ("entity_cn2en", "type_cn2en"):
            for cn in (d.get(key) or {}):
                if cn and len(cn) >= 2:
                    counter[cn] += 1
    return counter


def absorb_from_counter(counter, threshold=CROSS_KB_THRESHOLD, verbose=True):
    """从跨行业统计提炼公共概念, 返回待补充的 {键: {中文: 规范}} 建议。"""
    # 过滤出跨行业概念
    common = {cn: n for cn, n in counter.items() if n >= threshold}
    if verbose:
        print(f"跨行业概念(≥{threshold}个KB): {len(common)} 个")
    # 去噪: 过滤带行业前缀的污染词(如 阀门产品/化工设备/机械客户)
    # 这些是"行业名+通用概念"拼接, 不是干净的公共概念
    _INDUSTRY_PREFIXES = ("阀门","化工","机械","食品","船舶","五金","纺织","塑料","医疗",
                          "电子","家电","汽车","汽配","精密","波纹管","机床","能源","电力",
                          "地震","测井","制造","工业")
    def _is_polluted(cn):
        for p in _INDUSTRY_PREFIXES:
            if cn.startswith(p) and cn != p and len(cn) > len(p):
                return True
        return False
    common = {cn: n for cn, n in common.items() if not _is_polluted(cn)}
    if verbose:
        print(f"去噪后公共概念: {len(common)} 个")
    # 映射到公共层键位(启发式): 依据现有公共词典的分类 + 常见词性
    suggestions = {"entity_cn2en": {}, "type_cn2en": {}}
    for cn, n in sorted(common.items(), key=lambda x: -x[1]):
        # entity(实体): 设备/产品/客户/批次/原料 等通用业务实体
        if cn in ("设备","产品","客户","供应商","批次","原料","原材料","销售","质检","库存","订单","合同"):
            suggestions["entity_cn2en"][cn] = cn.lower()
        # type(类型): 设备类型/产品类型
        elif any(k in cn for k in ("设备","装置","阀","泵","机","炉","塔","器","车床","铣床")):
            suggestions["type_cn2en"][cn] = cn
    return suggestions


def merge_into_public(public, suggestions):
    """把吸收到的公共概念合并进公共词典(KB 覆盖公共, 公共兜底 KB)。"""
    changed = False
    for key, mapping in suggestions.items():
        cur = public.get(key) or {}
        for cn, en in mapping.items():
            if cn not in cur:
                cur[cn] = en
                changed = True
        public[key] = cur
    return public, changed


def main():
    if "--stats" in sys.argv:
        pub = load_public()
        for k in ("type_cn2en","status_cn2en","synonym_map","entity_cn2en","fault_cn2en","product_type_cn2en"):
            print(f"  {k}: {len(pub.get(k,{}))}")
        return

    if "--scan" in sys.argv:
        print("=== 扫描全部 KB, 提炼跨行业公共概念 ===")
        counter = scan_kb_lexicons()
        suggestions = absorb_from_counter(counter)
        public = load_public()
        public, changed = merge_into_public(public, suggestions)
        if changed:
            with open(PUBLIC_MAIN, "w", encoding="utf-8") as f:
                json.dump(public, f, ensure_ascii=False, indent=2)
            print(f"已补充到公共词典: {PUBLIC_MAIN}")
            for key, mp in suggestions.items():
                if mp:
                    print(f"  {key}: +{len(mp)} 个概念")
        else:
            print("无新增(公共词典已含这些概念)")
        return

    if "--kb" in sys.argv:
        idx = sys.argv.index("--kb")
        kb_path = sys.argv[idx + 1]
        print(f"=== 增量吸收企业词典: {kb_path} ===")
        d = load_json(kb_path)
        if not d:
            print("词典加载失败"); return
        # 单企业吸收: 该企业词典里的通用概念(在公共层已有或属通用词)
        counter = Counter()
        for key in ("entity_cn2en","type_cn2en"):
            for cn in (d.get(key) or {}):
                if cn and len(cn) >= 2:
                    counter[cn] += 1
        suggestions = absorb_from_counter(counter, threshold=1, verbose=False)
        public = load_public()
        public, changed = merge_into_public(public, suggestions)
        if changed:
            with open(PUBLIC_MAIN, "w", encoding="utf-8") as f:
                json.dump(public, f, ensure_ascii=False, indent=2)
            print("已增量吸收")
        else:
            print("无新增")
        return

    print(__doc__)


if __name__ == "__main__":
    main()
