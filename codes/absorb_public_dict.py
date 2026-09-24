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
import hashlib
import datetime
from collections import Counter

# 本模块目录 = codes/
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
PUBLIC_DIR = os.path.join(ROOT, "industrial_dict")
# 行业词典映射: 行业 -> 词典文件
INDUSTRY_FILES = {
    "基础": "00_basis.json",
    "泵阀": "01_valve_pump.json",
    "精细化工": "02_fine_chem.json",
    "地球物理": "03_geophysics.json",
}

# ── 词典分层标记（KB 注册表 + 词典分层 2026-09-24）─────────────────────────────
# 本模块只写「公共工业本体层」（PUBLIC_DIR，只读语义，开源算法资产），
# 工厂专属层见 dict_asset.py（DICT_LAYER="factory"，写 config/lexicon_<kb>.json）。
DICT_LAYER = "public"
PUBLIC_LAYER_DIR = PUBLIC_DIR          # 只读层目录，禁止工厂私有词写入（见 dict_asset.py 守卫）
FACTORY_LAYER_DIR = CONFIG_DIR         # 工厂专属层目录（可写），仅作标注

# 公共词典合并键位（只吸收这几类；attr/numeric 属工厂字段，不吸收入公共层）
_MERGE_KEYS = ("type_cn2en", "status_cn2en", "synonym_map", "entity_cn2en",
               "fault_cn2en", "material_synonyms", "pump_cn2en", "part_cn2en",
               "process_cn2en", "product_type_cn2en", "safety_cn2en", "method_cn2en")
# 可作为公共概念候选的词表键（企业词典里这些键的中文词才参与吸收）
_ABSORB_KEYS = ("type_cn2en", "entity_cn2en", "fault_cn2en", "status_cn2en",
                "material_synonyms", "pump_cn2en", "part_cn2en", "process_cn2en",
                "product_type_cn2en", "safety_cn2en", "method_cn2en")
# 吸收判定阈值: 概念出现在 ≥N 个「独立来源」才视为公共概念候选
#   —— 独立来源 = 词集合 Jaccard < SAME_SOURCE_JACCARD 的批次（模板复制的多份只算 1 个）
CROSS_KB_THRESHOLD = 3
SAME_SOURCE_JACCARD = 0.9
# 候选池：未达阈值的概念先落这里（记来源与首见时间），达阈值才升级进行业层
CAND_PATH = os.path.join(PUBLIC_DIR, "_candidates.json")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_public(industry=None):
    """加载公共词典。industry 指定行业(泵阀/精细化工/地球物理/基础)加载对应文件;
    默认加载基础层 00_basis。"""
    fn = INDUSTRY_FILES.get(industry, "00_basis.json")
    d = load_json(os.path.join(PUBLIC_DIR, fn))
    if not d:
        d = {
            "description": f"公共工业本体词典（{industry or '基础'}）",
            "version": "1.0.0",
            "built": "2026-08-18",
            "type_cn2en": {}, "status_cn2en": {}, "synonym_map": {},
            "entity_cn2en": {}, "fault_cn2en": {},
        }
    return d


def save_public(d, industry=None):
    fn = INDUSTRY_FILES.get(industry, "00_basis.json")
    with open(os.path.join(PUBLIC_DIR, fn), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return os.path.join(PUBLIC_DIR, fn)


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


def lexicon_word_set(d, keys=_ABSORB_KEYS):
    """企业词典 → {(词表键, 中文词)} 集合（只取可吸收的词表键，不含工厂字段）。"""
    ws = set()
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            for w in v:
                if isinstance(w, str) and len(w) >= 2:
                    ws.add((k, w))
    return ws


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _public_word_set():
    """公共层 + 行业层已有的词集合。

    聚类时必须从企业词典里剔除它们：词典生成时已并入公共层，人人都含公共词，
    不剔除会让所有词典两两 Jaccard 虚高 → 全部误判为「同源」。
    """
    ws = set()
    for fn in set(INDUSTRY_FILES.values()):
        d = load_json(os.path.join(PUBLIC_DIR, fn)) or {}
        ws |= lexicon_word_set(d)
    return ws


def source_clusters(lex_paths=None, similarity=SAME_SOURCE_JACCARD, exclude_public=True):
    """把多份企业词典按「自身词集合 Jaccard ≥ similarity」聚成独立来源簇。

    同簇 = 同一来源（模板复制/同一批生成），独立来源数按簇计，不按文件数计
    —— 治 D3：47 份词典里 35 份是同一分钟批量生成的，文件计数会把模板当共识。
    exclude_public=True 时先剔除公共/行业层已有的词（只比企业私有词），否则
    "人人都含公共词"会让 Jaccard 虚高、把不同企业误判成同源。
    返回 [{"paths": [...], "ws": {(键,词)}}...]
    """
    if lex_paths is None:
        lex_paths = glob.glob(os.path.join(CONFIG_DIR, "lexicon_*.json"))
    pub_ws = _public_word_set() if exclude_public else set()
    clusters = []
    for p in sorted(lex_paths):
        d = load_json(p)
        if not d:
            continue
        ws = lexicon_word_set(d) - pub_ws
        if not ws:
            continue
        placed = False
        for c in clusters:
            if _jaccard(ws, c["ws"]) >= similarity:
                c["paths"].append(p)
                c["ws"] |= ws
                placed = True
                break
        if not placed:
            clusters.append({"paths": [p], "ws": set(ws)})
    return clusters


def independent_source_counter(lex_paths=None, exclude_public=True):
    """统计每个词出现在多少个「独立来源（簇）」中。

    返回 (Counter{词: 独立来源数}, 诊断 dict)
    """
    clusters = source_clusters(lex_paths, exclude_public=exclude_public)
    counter = Counter()
    for c in clusters:
        for w in set(w for _, w in c["ws"]):
            counter[w] += 1
    diag = {
        "files": sum(len(c["paths"]) for c in clusters),
        "independent_sources": len(clusters),
        "clusters": [sorted(os.path.basename(x) for x in c["paths"]) for c in clusters],
    }
    return counter, diag


def load_candidates():
    d = load_json(CAND_PATH)
    return d if isinstance(d, dict) else {}


def save_candidates(d):
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with open(CAND_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)
    return CAND_PATH


def promote_candidates(counter, threshold=CROSS_KB_THRESHOLD, industry="基础",
                       key_map=None, verbose=True):
    """把独立来源数 ≥ threshold 的词升级进 industry 行业层。返回 {键: 新增词数}。"""
    suggestions = absorb_from_counter(counter, threshold=threshold, verbose=verbose, key_map=key_map)
    pub = load_public(industry)
    pub, changed = merge_into_public(pub, suggestions)
    out = {}
    if changed:
        save_public(pub, industry)
        for k, mp in suggestions.items():
            if mp:
                out[k] = len(mp)
    return out


def learn_from_kb(kb_path, industry="基础", threshold=CROSS_KB_THRESHOLD, verbose=True):
    """服务完一个企业后：把该企业词典的词记入候选池（记来源/首见），
    只有累计到 threshold 个「独立来源」的概念才升级进 industry 行业层。

    返回值 {"ok","source","pool_added","independent_sources","promoted","threshold"}。
    """
    d = load_json(kb_path)
    if not d:
        return {"ok": False, "error": "词典加载失败: %s" % kb_path}
    ws = lexicon_word_set(d)
    if not ws:
        return {"ok": False, "error": "词典不含可吸收词表键（%s）" % ", ".join(_ABSORB_KEYS)}
    src = os.path.splitext(os.path.basename(kb_path))[0]
    today = datetime.date.today().isoformat()

    # 独立来源计数：把本次词典并入全库后按簇重算（同源模板不重复计）
    all_paths = [p for p in glob.glob(os.path.join(CONFIG_DIR, "lexicon_*.json"))
                 if os.path.abspath(p) != os.path.abspath(kb_path)]
    all_paths.append(kb_path)
    counter, diag = independent_source_counter(all_paths)

    # 候选池：记来源与首见时间（未达阈值的词也留痕，供后续复核）
    cand = load_candidates()
    pool_added = 0
    key_map = {}
    for k, w in ws:
        key_map.setdefault(w, k)
        ent = cand.setdefault(w, {"sources": [], "first_seen": today, "last_seen": today, "key": k})
        ent["last_seen"] = today
        if src not in ent["sources"]:
            ent["sources"].append(src)
            pool_added += 1
    save_candidates(cand)

    promoted = promote_candidates(counter, threshold=threshold, industry=industry,
                                 key_map=key_map, verbose=verbose)
    return {"ok": True, "source": src, "pool_added": pool_added, "pool_size": len(cand),
            "independent_sources": diag["independent_sources"], "clusters": diag["clusters"],
            "promoted": promoted, "threshold": threshold}


def absorb_from_counter(counter, threshold=CROSS_KB_THRESHOLD, verbose=True, key_map=None):
    """从「词的独立来源计数」提炼公共概念, 返回待补充的 {键: {中文: 规范}} 建议。

    counter: {词: 独立来源数}（independent_source_counter 产出；旧的文件计数仍可传，语义降级）
    key_map: 可选 {词: 原词表键}，优先按来源键归类（比启发式准）
    """
    # 过滤出跨行业概念
    common = {cn: n for cn, n in counter.items() if n >= threshold}
    if verbose:
        print("跨行业概念(≥%d 个独立来源): %d 个" % (threshold, len(common)))
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
        print("去噪后公共概念: %d 个" % len(common))
    # 归类: 优先按来源键（key_map），无来源键时走启发式
    suggestions = {}
    for cn, n in sorted(common.items(), key=lambda x: -x[1]):
        k = (key_map or {}).get(cn)
        if k in _MERGE_KEYS:
            suggestions.setdefault(k, {})[cn] = cn
            continue
        # entity(实体): 设备/产品/客户/批次/原料 等通用业务实体
        if cn in ("设备","产品","客户","供应商","批次","原料","原材料","销售","质检","库存","订单","合同"):
            suggestions.setdefault("entity_cn2en", {})[cn] = cn.lower()
        # type(类型): 设备类型/产品类型
        elif any(k2 in cn for k2 in ("设备","装置","阀","泵","机","炉","塔","器","车床","铣床")):
            suggestions.setdefault("type_cn2en", {})[cn] = cn
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


def _get_industry():
    if "--industry" in sys.argv:
        return sys.argv[sys.argv.index("--industry") + 1]
    return None


def main():
    if "--stats" in sys.argv:
        ind = _get_industry()
        pub = load_public(ind)
        print(f"行业词典 [{ind or '基础'}]:")
        for k in ("type_cn2en","status_cn2en","synonym_map","entity_cn2en","fault_cn2en","pump_cn2en","standards"):
            print(f"  {k}: {len(pub.get(k,{}))}")
        return

    if "--export" in sys.argv:
        ind = _get_industry()
        pub = load_public(ind)
        fn = INDUSTRY_FILES.get(ind, "00_basis.json")
        # 导出: 拷贝到独立导出目录(供服务方积累)
        export_dir = os.path.join(ROOT, "..", "dict_export")
        os.makedirs(export_dir, exist_ok=True)
        out = os.path.join(export_dir, fn)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(pub, f, ensure_ascii=False, indent=2)
        print(f"已导出行业词典 → {out}")
        print(f"  文件: {fn}")
        print(f"  type: {len(pub.get('type_cn2en',{}))}, status: {len(pub.get('status_cn2en',{}))}")
        return

    if "--scan" in sys.argv:
        print("=== 扫描全部 KB 词典, 按「独立来源」提炼公共概念 ===")
        counter, diag = independent_source_counter()
        print("  词典 %d 份 → 独立来源 %d 簇（同源模板只计 1 个来源）"
              % (diag["files"], diag["independent_sources"]))
        for i, c in enumerate(diag["clusters"], 1):
            print("    簇%d(%d份): %s" % (i, len(c), "、".join(c[:6]) + ("…" if len(c) > 6 else "")))
        promoted = promote_candidates(counter, threshold=CROSS_KB_THRESHOLD, industry="基础")
        if promoted:
            print("已升级进基础层 00_basis.json（独立来源 ≥ %d）:" % CROSS_KB_THRESHOLD)
            for k, n in promoted.items():
                print("  %s: +%d" % (k, n))
        else:
            print("无可升级概念（独立来源数均 < %d）" % CROSS_KB_THRESHOLD)
        return

    if "--candidates" in sys.argv:
        cand = load_candidates()
        print("候选池 %d 词  %s" % (len(cand), CAND_PATH))
        for w, e in sorted(cand.items(), key=lambda x: -len(x[1].get("sources", [])))[:30]:
            print("  %-12s 来源%d  %s" % (w, len(e.get("sources", [])), e.get("sources", [])[:4]))
        return

    if "--kb" in sys.argv or "--learn" in sys.argv:
        flag = "--kb" if "--kb" in sys.argv else "--learn"
        kb_path = sys.argv[sys.argv.index(flag) + 1]
        ind = _get_industry() or "基础"
        print("=== 服务完一个企业：%s → 候选池 → %s行业层（达阈值才升级）===" % (kb_path, ind))
        res = learn_from_kb(kb_path, industry=ind)
        if not res.get("ok"):
            print("  " + res.get("error", "失败"))
            return
        print("  来源 %s：候选池 +%d 词（共 %d 词）" % (res["source"], res["pool_added"], res["pool_size"]))
        print("  全库独立来源 %d 簇：%s" % (res["independent_sources"],
              "、".join("%s(%d份)" % (c[0], len(c)) for c in res["clusters"][:5])))
        if res["promoted"]:
            print("  已升级进 %s 行业层（独立来源 ≥ %d）:" % (ind, res["threshold"]))
            for k, n in res["promoted"].items():
                print("    %s: +%d" % (k, n))
        else:
            print("  无升级：单企业不足以判定公共概念（需 ≥%d 个独立来源）；词已入候选池待后续企业确认"
                  % res["threshold"])
        return

    print(__doc__)


if __name__ == "__main__":
    main()
