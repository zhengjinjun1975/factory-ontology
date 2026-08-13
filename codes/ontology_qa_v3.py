#!/usr/bin/env python3
"""ontology_qa_v3.py — 词典驱动的通用问答引擎（精简方法论核心）。

设计目标：换任何数据源/工厂，只换词典，引擎代码不动。
原则：模板逻辑通用，不硬编码任何具体中文词；属性名、枚举值全部从词典读。

词典 (lexicon.json) 提供：
  attr_cn2en   中文属性名 -> 字段英文 (如 功率->powerKw)
  attr_en2cn   字段英文 -> 中文属性名 (显示用)
  status_cn2en 状态词 -> 值
  type_cn2en   类型词 -> 值
  zone_cn2en   区域词 -> 值 (可选)
  field_aliases 标准字段别名 (status/deviceType/deviceName/location)

通用模板（与具体词无关）：
  数量: "多少[台/个/条][状态词/类型词/区域词]" 或 "[词]的[词]共几"
  列出: "列出所有[类型/状态/区域]"
  极值: "[属性]最[大/小/高/低]"
  TopN: "[属性]最[高/低]的N[台/个]"
  平均: "[属性]平均"
  总和: "[属性]总[和/计]"
  范围: "[属性][大于/小于/在A到B之间]"
  分组: "统计按[属性]" 或 "按[属性]和[属性]统计"
  反查: "哪些[设备/物品][属性]=值"
  组合: "[状态词]的[类型词]"

用法: python ontology_qa_v3.py <nt文件> "<问题>" [lexicon.json]
"""

import sys
import os
import re
import json
from collections import defaultdict

# 词典数据收敛到 lexicon.py 唯一数据源（P1-7），仅持有只读引用，消除跨文件重复漂移。
from lexicon import get_attr_cn_aliases, get_common_zh_status, get_entity_cn2uri


# ------------------------------------------------------------------ 词典加载

def load_dict(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 解析(复用)

def parse_nt(nt_file):
    """解析 N-Triples。返回 (s,p,o) 列表。"""
    triples = []
    with open(nt_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            m = re.match(r'^<([^>]*)>\s+<([^>]*)>\s+(.+?)\s*\.\s*$', line)
            if not m:
                continue
            s, p = m.group(1), m.group(2)
            o_raw = m.group(3).strip()
            om = re.match(r'^<([^>]*)>$', o_raw)
            if om:
                o = om.group(1)
            else:
                lm = re.match(r'^"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]*)>)?', o_raw)
                o = lm.group(1) if lm else o_raw
            triples.append((s, p, o))
    return triples


def build_data(triples, dict_data):
    """三元组 -> {实例名: {属性: 值}}。字段名用尾部局部名。"""
    def tail(uri):
        return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]

    # 自动发现实体类：多表建模含多个类。类名是 "X type owl:Class" 的 subject
    classes = {tail(s) for s, p, o in triples
               if tail(p) == "type" and tail(o) == "Class"}
    if not classes:
        return {}
    individuals = set()
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) in classes:
            individuals.add(s)
    data = {tail(i): {} for i in individuals}
    for s, p, o in triples:
        sn = tail(s)
        # 只跳过 RDF 命名空间的 rdf:type，保留本体数据属性 type（产品类型等）
        if sn in data and not p.rstrip(">").endswith("rdf-syntax-ns#type"):
            data[sn][tail(p)] = o
    return data


# ------------------------------------------------------------------ 通用工具

def _field(rec, canonical, aliases):
    """按字段别名取标准字段值。canonical 如 status/deviceType/deviceName/location。
    别名找不到时，兜底匹配 csv_to_owl 驼峰化后的字段名(pump_status→pumpStatus)。
    再兜底做大小写不敏感的蛇形/驼峰归一化匹配(expiry_days↔expiryDays)。"""
    for alias in aliases.get(canonical, [canonical]):
        if alias in rec:
            return rec[alias]
    # 驼峰兜底: 对每个别名尝试驼峰化
    for alias in aliases.get(canonical, [canonical]):
        parts = [p for p in alias.replace("-", "_").split("_") if p]
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        if camel in rec:
            return rec[camel]
    # 大小写不敏感归一化兜底: 字段名去下划线+小写后匹配(expiry_days↔expiryDays)
    canon_norm = canonical.replace("_", "").lower()
    rn = {k.replace("_", "").lower(): v for k, v in rec.items()}
    if canon_norm in rn:
        return rn[canon_norm]
    return rec.get(canonical, "")


def _type_matches(d, ty_en, aliases):
    """记录 d 是否属于类型 ty_en（值级判定）。

    优先规范字段(deviceType/type/category)精确匹配；未命中时兜底扫描全部字段——
    精确匹配优先，其次"取值包含"（容忍 304不锈钢 vs 不锈钢）。这使"XX类的产品"查询
    不再依赖字段名必须是 type/category（material/pressure_grade/设备类型等兜底字段也能命中），
    配合 lexicon_agent 自动把低基数枚举兜底进 type_cn2en，实现甲方自助补全词典。
    """
    for f in ("deviceType", "category", "type", "kind"):
        if _field(d, f, aliases) == ty_en:
            return True
    if str(d.get("deviceType", "") or d.get("category", "") or d.get("type", "")) == ty_en:
        return True
    for v in d.values():
        if str(v) == ty_en:
            return True
    if ty_en:
        for v in d.values():
            if ty_en in str(v):
                return True
    return False


# 常见数值/极值字段的中文别名 -> 规范英文字段名（跨行业泛化，非硬编码具体行业）。
# 词典 attr_cn2en 缺失时兜底，覆盖高频中文口语：保质期/质保/寿命等。
# 数据来源 lexicon.get_attr_cn_aliases()（与原 _ATTR_CN_ALIASES 键值逐一一致）。
_ATTR_CN_ALIASES = get_attr_cn_aliases()


# 通用极值词 -> 目标数值字段中文关键词（跨行业泛化，非硬编码具体行业）。
# 极值模板"最X的Y"中 X 为形容词(贵/便宜/大/小/高/低)时，据此从 numeric_fields
# 推断 Y 对应的数值字段：贵/便宜→价格类，大/小→容量/吨位类，高/低→温度/功率/压力类。
_EXTREME_WORD_FIELDS = {
    "贵":   ["价格", "售价", "金额", "单价", "价钱", "成本", "投资"],
    "便宜": ["价格", "售价", "金额", "单价", "价钱", "成本", "投资"],
    "大":   ["容量", "尺寸", "吨位", "数量", "库存", "面积", "体积", "规模"],
    "小":   ["容量", "尺寸", "吨位", "数量", "库存", "面积", "体积", "规模"],
    "高":   ["温度", "价格", "功率", "压力", "高度", "转速", "电流", "电压", "水位", "速度"],
    "低":   ["温度", "价格", "功率", "压力", "高度", "转速", "电流", "电压", "水位", "速度"],
}


def _extreme_field(dict_data, q):
    """'最X的Y' 中 X 为通用极值词时，推断 Y 对应数值字段。
    从 numeric_fields(中文→字段) + attr_cn2en 中挑中文名含目标关键词的字段；
    按关键词长度降序取首个，保证"价格"先于"金额"等。返回 (en字段, 中文名) 或 (None,None)。"""
    m = re.search(r"最([贵便宜大小高低]+)[的]?", q)
    if not m:
        return None, None
    kws = _EXTREME_WORD_FIELDS.get(m.group(1))
    if not kws:
        return None, None
    cand = dict(dict_data.get("numeric_fields", {}) or {})
    cand.update({cn: en for cn, en in dict_data.get("attr_cn2en", {}).items()})
    for kw in sorted(kws, key=len, reverse=True):
        for cn in sorted(cand, key=len, reverse=True):
            if kw in cn:
                return cand[cn], cn
    return None, None


# CJK 统一表意文字范围 [U+4E00, U+9FFF], 用 chr() 构造避免源码转义被改写
_CJK_START, _CJK_END = chr(0x4E00), chr(0x9FFF)


def _strip_unit(cn):
    """去掉属性中文名末尾的单位后缀, 让口语能命中带单位后缀的词典键。

    支持括号单位('功率（千瓦）'→'功率')与无括号单位('功率kW'/'温度℃'/'尺寸mm'→
    '功率'/'温度'/'尺寸'), 从而 '功率最大的设备' 能命中词典键 '功率kW'→powerKw。
    仅当剥离后仍以中文开头才生效, 避免把纯英文字段名('batchsizel')剥空造成误匹配。
    """
    if not cn:
        return cn or ""
    stripped = re.sub(r"[（(][^（）()]*[）)]$", "", cn).strip()
    base = re.sub(r"[^%s-%s]+$" % (_CJK_START, _CJK_END), "", stripped)  # 去末尾非中文单位
    if _CJK_START <= base[:1] <= _CJK_END:
        return base.strip()
    return stripped  # 无中文则保留原样, 防误剥纯英文键


def _find_attr(dict_data, q):
    """从词典找问题里出现的属性中文词 -> 字段英文。按长度降序避免短词短路。

    兼容词典键带单位后缀('功率（千瓦）')也能被'功率'口语命中: 为每个 attr_cn2en 键
    额外生成去单位后缀的变体, 保证映射到可解析的规范字段(power_kw), 而非泛化别名
    (如'功率'→'power')。词典未命中时兜底 _ATTR_CN_ALIASES / numeric_fields / 极值推断。
    """
    cand = []  # (min_len, 中文匹配词, 英文字段); min_len 防单字别名误命中
    for cn, en in dict_data.get("attr_cn2en", {}).items():
        cand.append((1, cn, en))
        base = _strip_unit(cn)
        if base and base != cn:
            cand.append((1, base, en))
    for cn, en in _ATTR_CN_ALIASES.items():
        cand.append((2, cn, en))
    nf = dict_data.get("numeric_fields", {}) or {}
    for cn, en in nf.items():
        cand.append((1, cn, en))
    seen = set()
    # 按长度降序, 优先长词(完整键), 稳定排序保证同长时 attr_cn2en 变体先于泛化别名
    for min_len, cn, en in sorted(cand, key=lambda x: len(x[1]), reverse=True):
        key = (cn, en)
        if key in seen:
            continue
        seen.add(key)
        if len(cn) >= min_len and cn in q:
            return en, cn
    # 通用极值词兜底: '最X的Y' 中 X 为形容词(贵/便宜/大/小/高/低)时推断 Y 的数值字段
    return _extreme_field(dict_data, q)


def _num(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _display_name(rec, aliases, default=""):
    """显示名：优先 deviceName；无名称字段时兜底 id/UDI/主键，最后用 key。"""
    n = _field(rec, "deviceName", aliases)
    if n:
        return n
    # 兜底: 尝试常见主键字段 (含驼峰)
    for key in ("id", "udi", "UDI", "device_id", "serial_no", "code", "name",
                "product_name", "productName", "raw_name", "rawName",
                "unit_name", "unitName", "equipment_name", "equipmentName"):
        if key in rec:
            return rec[key]
    return default


# 内置中文状态词 → 英文值兜底（当词典缺失中文映射时，覆盖高频运维词汇）。
# 数据来源 lexicon.get_common_zh_status()（与原 _COMMON_ZH_STATUS 键值逐一一致）。
_COMMON_ZH_STATUS = get_common_zh_status()


def _find_enum(dict_data, q, which):
    """从词典找问题里出现的枚举词 -> 值。which in (status/type/zone)。
    增强：status 词在词典缺中文映射时，用内置中文→英文兜底。
    增强：LLM 语义聚类 synonym_map 展开——问题含同义词/别名时命中规范词（乳制品→奶制品）。"""
    key = f"{which}_cn2en"
    for cn, en in sorted(dict_data.get(key, {}).items(), key=lambda x: len(x[0]), reverse=True):
        if cn in q:
            return en, cn
    # 同义词展开：LLM 语义聚类 synonym_map 反查——问题含某词组任一同义/别名时，映射回规范词对应的枚举值
    smap = dict_data.get("synonym_map", {}) or {}
    if smap:
        en_map = dict_data.get(key, {})
        reverse = {}  # 同义词/别名 -> 枚举规范词
        for canon, group in smap.items():
            for m in [canon] + list(group):
                if m:
                    reverse.setdefault(m, canon)
        for term in sorted(reverse, key=len, reverse=True):
            if term and term in q:
                canon = reverse[term]
                if canon in en_map:
                    return en_map[canon], canon
                # 规范词不在枚举键中，但同义词可能直接等于某枚举键
                for ck in en_map:
                    if ck in [canon] + list(smap.get(canon, [])):
                        return en_map[ck], ck
    # status 兜底：中文运维词 → 英文值（词典可能只有英文键，如 {running:running}）
    if which == "status":
        for cn, en in sorted(_COMMON_ZH_STATUS.items(), key=lambda x: len(x[0]), reverse=True):
            if cn in q:
                return en, cn
    return None, None


def _extract_nums(q):
    return [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)', q)]


def _fmt_names(names, limit=20):
    shown = names[:limit]
    out = "\n".join("  - " + n for n in shown)
    if len(names) > limit:
        out += f"\n  ... 共 {len(names)} 条"
    return out


def _value_filter_count(q, data):
    """属性值过滤计数（泛化）：形如"XX的数量/多少"——问题含某属性的取值(或其公共子串)时统计符合行。
    - 提取问题核心片段(去掉 数量/多少/共/总 等量词)，取其 2~3 字公共子串作为过滤指纹；
    - 对每个记录的各文本取值，若与指纹有>=2字公共子串则视为该字段命中；
    - 跨多个字段命中(如材质+名称：不锈钢波纹管)按 AND 取交集返回；单字段命中按该字段计数。
    data profiling 未覆盖的复合过滤（材质/名称/类型取值）走此兜底，命中即返回，避免"暂不支持"。
    返回 (count, 描述) 或 None(无命中)。"""
    core = re.sub(r'(的数量|有多少个|有多少|多少个|共有多少|共多少|数量|多少|总数|共|总)', '', q)
    core = core.strip().replace(" ", "")
    if len(core) < 2 or not re.search(r'(数量|多少)', q):
        return None
    grams = set()
    for i in range(len(core)):
        for L in (2, 3):
            if i + L <= len(core):
                grams.add(core[i:i + L])
    if not grams:
        return None
    records = []  # (name, 命中的字段集合)
    for n, d in data.items():
        fields = set()
        for k, v in d.items():
            vs = str(v)
            if len(vs) < 2 or _num(vs) is not None:
                continue
            if any(g in vs for g in grams):
                fields.add(k)
        records.append((n, fields))
    strong = [(n, f) for n, f in records if len(f) >= 2]
    weak = [(n, f) for n, f in records if len(f) == 1]
    if strong:
        return len(strong), "多字段(材质+名称等)"
    if weak:
        return len(weak), "单字段取值"
    return None


# ------------------------------------------------------------------ 问答主逻辑

_EXTREME = re.compile(r"最(大|高|多|低|小|少|长|短|贵|便宜|快|慢|久|重|轻|新|老|早|晚|近)")
_EXTREME_MAX = ("最大","最高","最多","最长","最贵","最快","最久","最重","最新","最老")
_EXTREME_MIN = ("最小","最低","最少","最短","最慢","最轻","最早","最便宜")


def _is_max(q):
    """问题是否求极值中的最大值。MIN 词(最便宜/最短/最慢等)优先按最小。"""
    if _is_min(q):
        return False
    for k in _EXTREME_MAX:
        if k in q:
            return True
    # 含"最"但未列出的, 按常识多数求最大
    return _EXTREME.search(q) is not None


def _is_min(q):
    return any(k in q for k in _EXTREME_MIN)


# ------------------------------------------------------------------ 通用跨域校验
# 数据查询意图正则(计数/列表/极值/范围/统计等)。跨域校验只拦"明确的数据查询",
# 不拦开放式/咨询问题(那些走 LLM 兜底生成建议)。
_DATA_QUERY_RE = re.compile(
    r"多少|数量|总数|几个|总共|共计|共有|一共有|列出|有哪些|哪些|"
    r"最[大小高低多少长短贵便宜快慢久重轻新老早晚近]|共\s*多少|"
    r"是多少|等于|大于|小于|高于|低于|平均|合计|总和|统计|分组|"
    r"台|条|个|张|艘|本|支|卷|份")


def kb_vocab(D):
    """该 kb 本体的全部领域词(中文实体/类型/状态/区域/属性/数值字段等)。

    跨域判定依据: 词典 entity_cn2en(实体类) + type/status/zone/category 枚举 +
    attr_cn2en/attr_en2cn(属性) + numeric_fields。只收集该 kb 词典里真实出现的概念,
    不依赖任何白名单词表 —— 换任何行业/数据源都通用。
    """
    voc = set()
    for key in ("entity_cn2en", "type_cn2en", "status_cn2en", "zone_cn2en",
                "category_cn2en", "attr_cn2en", "attr_en2cn"):
        m = (D or {}).get(key) or {}
        for k in m:
            if k:
                voc.add(k)
            v = m[k]
            if isinstance(v, str) and v:
                voc.add(v)
    for k in ((D or {}).get("numeric_fields") or {}):
        if k:
            voc.add(k)
    return {w for w in voc if w and len(str(w)) >= 1}


def is_cross_domain_data_query(q, D):
    """通用跨域校验(取代原白名单词表钩子)。

    若问题是一条"明确的数据查询"(计数/列表/极值/范围/统计), 且其中引用的实体概念
    不在该 kb 本体任何实体类/词典(即问题里不含任何该 kb 领域词), 判定为跨域 →
    应禁止 LLM 兜底编造, 强制返回"无相关数据"。
    横向覆盖所有跨域问题(问书/船/测线/冲床/图纸…), 不靠具体词表。
    非数据查询(开放式/咨询)返回 False, 不拦截。
    """
    if not q or not D:
        return False
    if not _DATA_QUERY_RE.search(q):
        return False  # 非数据查询, 不拦
    voc = kb_vocab(D)
    if not voc:
        return False
    for w in voc:
        if w and w in q:
            return False  # 问题含该 kb 领域词 → 域内, 不拦
    return True


def _entity_subset(q, D, data):
    """实体消歧: 若问题含 entity_cn2en 的实体词(如"机组"), 返回该实体类的实例子集;
    否则返回全部。解决"运行中的机组"误匹配到设备(锅炉/发电机)而非机组。"""
    emap = dict(get_entity_cn2uri())
    emap.update(D.get("entity_cn2en", {}) or {})
    for cn in sorted(emap, key=len, reverse=True):
        if cn and cn in q:
            uri_sub = emap[cn].lower()
            sub = {k: d for k, d in data.items() if uri_sub in k.lower()}
            if sub:
                return sub
    return data


def answer(q, data, D):
    """词典 D 驱动的通用问答。"""
    aliases = D.get("field_aliases", {})
    cn2cn = D.get("attr_en2cn", {})

    # 显示名辅助
    def names(matched):
        return [_display_name(d, aliases, default=n) for n, d in matched]

    # ---- 组合: [状态]的[类型] (报警的焊接机器人) ----
    st_en, st_cn = _find_enum(D, q, "status")
    ty_en, ty_cn = _find_enum(D, q, "type")
    if st_en and ty_en:
        matched = [(n, d) for n, d in data.items()
                   if _field(d, "status", aliases) == st_en and _type_matches(d, ty_en, aliases)]
        nm = names(matched)
        if "多少" in q:
            return "有 %d %s的%s" % (len(nm), st_cn, ty_cn)
        if "列出" in q:
            return "列出所有%s的%s:\n%s" % (st_cn, ty_cn, _fmt_names(nm)) if nm else "无%s的%s" % (st_cn, ty_cn)
        return "%s的%s共 %d" % (st_cn, ty_cn, len(nm))

    # ---- 区域关系 ----
    zo_en, zo_cn = _find_enum(D, q, "zone")
    if zo_en and any(k in q for k in ("区域", "区的", "在", "哪些", "有哪些")):
        def _zmatch(d):
            for f in ("location", "zone", "region", "area", "workshop"):
                if _field(d, f, aliases) == zo_en:
                    return True
            return False
        matched = [(n, d) for n, d in data.items() if _zmatch(d)]
        nm = names(matched)
        if "多少" in q:
            return "%s区域有 %d" % (zo_cn, len(nm))
        return "%s区域的记录(%d):\n%s" % (zo_cn, len(nm), _fmt_names(nm))

    attr_en, attr_cn = _find_attr(D, q)

    # ---- 过滤计数: 属性=N 的数量 (机器故障标签=1 的数量) ----
    # 必须排在状态/类型词模板之前: 属性中文名可能含状态词(如"故障"),
    # 若被状态模板先命中会返回"有 N 故障的", 过滤计数永远不达。
    m_eq = re.search(r'[=＝]\s*(-?\d+(?:\.\d+)?)\s*的?\s*(数量|多少|共)', q)
    if attr_en and m_eq:
        target = m_eq.group(1)
        tv = float(target)
        n = sum(1 for d in data.values()
                if _num(_field(d, attr_en, aliases)) is not None
                and float(_num(_field(d, attr_en, aliases))) == tv)
        if n == 0:  # 可能以字符串存储
            n = sum(1 for d in data.values() if str(_field(d, attr_en, aliases)).strip() == target)
        cname = attr_cn or cn2cn.get(attr_en, attr_en)
        return "%s=%s 的数量是 %d" % (cname, target, n)

    # ---- 实体总数: 有多少台设备/产品总数 等 (实体类, 非类型值) ----
    # 实体类中文名 -> URI 子串。仅当问题里"多少[台个条]/总数/共多少"直接修饰实体类词时才触发，
    # 从而不误伤"有多少台空压机"(空压机是 deviceType 值, 走下方类型模板)。
    # 优先用建库时 data profiling 生成的 entity_cn2en {中文实体名: 表名}（测线/炮点/项目/船…），
    # 兜底保留原硬编码映射（兼容旧词典）。
    # 数据来源 lexicon.get_entity_cn2uri()（与原 _ENTITY_CN2URI 键值逐一一致）。
    _ENTITY_CN2URI = get_entity_cn2uri()
    _entity_map = dict(_ENTITY_CN2URI)
    _entity_map.update(D.get("entity_cn2en", {}) or {})
    # 计数量词：按实体词选择合适量词（测线→条 / 船→艘 / 项目→个 / 设备→台），缺省"个"
    _MEASURE = {"设备": "台", "测线": "条", "线": "条", "船": "艘", "产品": "个", "书": "本",
                "图书": "本", "项目": "个", "订单": "个", "批次": "批", "客户": "家", "炮点": "个", "质检": "个"}
    for cn in sorted(_entity_map, key=len, reverse=True):
        if (re.search(r'多少[台个条艘本]?' + cn, q)          # 有多少台设备 / 有多少本书
                or re.search(cn + r'(总数|共有多少|有多少|共多少)', q)  # 设备总数 / 炮点总数
                or re.search(r'共\s*多少\s*' + cn, q)):   # 共多少设备
            uri_sub = _entity_map[cn]
            n = sum(1 for k in data if uri_sub.lower() in k.lower())
            if "总数" in q:
                return "%s总数 %d" % (cn, n)
            return "有 %d %s%s" % (n, _MEASURE.get(cn, "个"), cn)

    # ---- 数量: 状态/类型/区域 ----
    st_en, st_cn = _find_enum(D, q, "status")
    if st_en and ("多少" in q or "数量" in q):
        sub = _entity_subset(q, D, data)  # 实体消歧: 只在该实体类实例中过滤
        n = sum(1 for d in sub.values() if _field(d, "status", aliases) == st_en)
        return "有 %d %s的" % (n, st_cn)
    ty_en, ty_cn = _find_enum(D, q, "type")
    if ty_en and ("多少" in q or "数量" in q):
        sub = _entity_subset(q, D, data)
        n = sum(1 for d in sub.values() if _type_matches(d, ty_en, aliases))
        return "有 %d %s" % (n, ty_cn)
    # 类型词 + 的：按类型过滤计数/列出（"大气治理的项目" / "油轮的" 等，非"多少"式）
    # 极值消歧(P1): "容量最大的发电机组"里"发电机"是 type_cn2en 子串会误命中此处,
    # 而问题实为"最X的Y"极值查询(attr_en 已解析 + 极值词)。极值应优先于类型列举,
    # 否则会错误返回"无发电机"。故类型列举仅在非极值查询时触发。
    if ty_en and "的" in q and not (_EXTREME.search(q) and attr_en):
        sub = _entity_subset(q, D, data)
        matched = [(n, d) for n, d in sub.items() if _type_matches(d, ty_en, aliases)]
        nm = names(matched)
        return "%s(%d):\n%s" % (ty_cn, len(nm), _fmt_names(nm)) if nm else "无%s" % ty_cn

    # ---- 列出 / 有哪些 / 信息 ----
    if "列出" in q or "哪些" in q or "有哪些" in q or "信息" in q or "详情" in q or ("类型" in q and "多少" not in q):
        # 问"XX类型/有哪些类型"且无具体值时 → 枚举该 type 的值(图书类型等)
        if "类型" in q and "哪些" not in q and "列出" not in q and "的" not in q:
            ty_vals = sorted({str(d.get("deviceType") or d.get("category") or "") for d in data.values()} - {""})
            if ty_vals:
                return "类型有：%s" % "、".join(ty_vals)
        st_en, st_cn = _find_enum(D, q, "status")
        if st_en:
            sub = _entity_subset(q, D, data)  # 实体消歧
            matched = [(n, d) for n, d in sub.items() if _field(d, "status", aliases) == st_en]
            return "列出所有%s:\n%s" % (st_cn, _fmt_names(names(matched))) if matched else "无%s" % st_cn
        ty_en, ty_cn = _find_enum(D, q, "type")
        if ty_en:
            matched = [(n, d) for n, d in data.items() if _type_matches(d, ty_en, aliases)]
            return "列出所有%s:\n%s" % (ty_cn, _fmt_names(names(matched))) if matched else "无%s" % ty_cn
        # 实体实例列表: "项目有哪些/订单有哪些/有哪些船" → 枚举 entity_cn2en 对应类的实例
        if ("有哪些" in q or "哪些" in q) and not st_en and not ty_en:
            emap = dict(get_entity_cn2uri()); emap.update(D.get("entity_cn2en", {}) or {})
            for cn in sorted(emap, key=len, reverse=True):
                if cn in q:
                    uri_sub = emap[cn].lower()
                    ents = sorted({k for k in data if uri_sub in k.lower()})
                    if ents:
                        names_list = [(_display_name(data[e], aliases, default=e.split('#')[-1])) for e in ents]
                        names_list = [n for n in names_list if n]
                        if names_list:
                            return "%s有：\n%s" % (cn, "、".join(names_list[:20]))
                        return "%s共 %d 个" % (cn, len(ents))

    # ---- TopN (属性最高/最低的N个) ----
    if attr_en and re.search(r'\d+\s*[台个条]', q) and _EXTREME.search(q):
        n = int(_extract_nums(q)[0]) if _extract_nums(q) else 3
        sub = _entity_subset(q, D, data)  # 极值消歧: 只在该实体类实例中求极值
        items = [(d, _num(_field(d, attr_en, aliases))) for d in sub.values()]
        items = [(d, v) for d, v in items if v is not None]
        is_max = _is_max(q)
        items.sort(key=lambda x: x[1], reverse=is_max)
        cname = attr_cn or cn2cn.get(attr_en, attr_en)
        rows = ["  - %s (%s=%s)" % (_display_name(d, aliases, default=""), cname, v) for d, v in items[:n]]
        return "%s%s的%d个:\n%s" % ("最高" if is_max else "最低", cname, n, "\n".join(rows))

    # ---- 单极值 (属性最大/最小/最长/最贵等) ----
    if attr_en and _EXTREME.search(q):
        # 语义陷阱拦截: "最X的Y" 里 Y 是抽象概念(安全/风险/措施/问题等)而非数值实体时,
        # 极值推断会把"最大的安全问题"误当成"容量最大"(只因为"最大"+数值字段匹配)。
        # 若问题含抽象概念词且无具体实体/属性指称, 不触发极值, 返回"暂不支持"走上层LLM兜底。
        _ABSTRACT_EXTREME = ("安全", "风险", "措施", "问题", "隐患", "方案", "建议", "原因",
                             "意义", "价值", "影响", "作用", "注意", "管理", "工作", "经验",
                             "挑战", "机会", "优势", "劣势", "趋势", "情况", "现状", "方向")
        _has_abstract = any(w in q for w in _ABSTRACT_EXTREME)
        _has_entity_ref = any(cn in q for cn in (D.get("entity_cn2en", {}) or {}))
        _has_attr_ref = any(cn in q for cn in (D.get("attr_cn2en", {}) or {}))
        if _has_abstract and not _has_entity_ref and not _has_attr_ref:
            return "暂不支持该问题"
        sub = _entity_subset(q, D, data)  # 极值消歧(P1): 只在该实体类实例中求极值
        items = [(d, _num(_field(d, attr_en, aliases))) for d in sub.values()]
        items = [(d, v) for d, v in items if v is not None]
        if items:
            is_max = _is_max(q)
            best = max(items, key=lambda x: x[1]) if is_max else min(items, key=lambda x: x[1])
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s的记录: %s (%s=%s)" % (("最大" if is_max else "最小") + cname, _display_name(best[0], aliases, default=""), cname, best[1])

    # ---- 平均 ----
    if attr_en and ("平均" in q or "均值" in q):
        vals = [_num(_field(d, attr_en, aliases)) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s平均值 %.2f (%d条)" % (cname, sum(vals)/len(vals), len(vals))

    # ---- 总和 ----
    if attr_en and any(k in q for k in ("总", "合计", "总和")):
        vals = [_num(_field(d, attr_en, aliases)) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s总和 %.2f" % (cname, sum(vals))

    # ---- 范围 (属性>N / <N / 在A到B) ----
    if attr_en:
        nums = _extract_nums(q)
        has_gt = any(k in q for k in ("大于", "高于", "超过", "以上"))
        has_lt = any(k in q for k in ("小于", "低于", "少于", "以下"))
        if ("到" in q or "之间" in q) and len(nums) >= 2:
            lo, hi = nums[0], nums[1]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and lo <= v <= hi]
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s在%s到%s之间的(%d):\n%s" % (cname, lo, hi, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))
        elif has_gt and nums:
            n = nums[0]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and v > n]
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s大于%s的(%d):\n%s" % (cname, n, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))
        elif has_lt and nums:
            n = nums[0]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and v < n]
            cname = attr_cn or cn2cn.get(attr_en, attr_en)
            return "%s小于%s的(%d):\n%s" % (cname, n, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))

    # ---- 分组统计 ----
    if "统计" in q or "分组" in q or ("按" in q and "和" in q):
        grp_attrs = []
        parts = re.findall(r'按([^\s和]+)', q)
        for p in parts:
            en = D.get("attr_cn2en", {}).get(p.strip())
            if en:
                grp_attrs.append(en)
        # 处理 "按X和Y"
        if not grp_attrs and "和" in q and "按" in q:
            pieces = [x.strip() for x in q.split("和") if x.strip()]
            for p in pieces:
                p2 = p.replace("按", "")
                en = D.get("attr_cn2en", {}).get(p2)
                if en:
                    grp_attrs.append(en)
        if grp_attrs:
            cnt = defaultdict(int)
            for d in data.values():
                key = tuple(_field(d, a, aliases) or "?" for a in grp_attrs)
                cnt[key] += 1
            cn_grp = [cn2cn.get(a, a) for a in grp_attrs]
            return "按%s统计:\n%s" % ("和".join(cn_grp), "\n".join(f"  {'/'.join(k)}: {v}条" for k, v in cnt.items()))

    # ---- 反查: 哪些[属性]=值 ----
    if attr_en and ("哪些" in q or "是什么" in q):
        matched = [(d, _field(d, attr_en, aliases)) for d in data.values() if _field(d, attr_en, aliases)]
        cname = attr_cn or cn2cn.get(attr_en, attr_en)
        return "%s信息:\n%s" % (cname, "\n".join(f"  {_display_name(d, aliases, default='')}: {v}" for d, v in matched[:20]))

    # ---- 属性值过滤计数 (泛化): "不锈钢波纹管的数量" / "船坞的数量" ----
    # data profiling 未覆盖的复合/取值过滤计数兜底（位于所有具名模板之后）。
    # 去掉调试后缀("单字段取值"), 给用户可读的计数回答。
    _vc = _value_filter_count(q, data)
    if _vc:
        return "符合条件共 %d 条" % _vc[0]

    # ---- 总数: 一共有多少条记录 ----
    if ("一共" in q or "总共有" in q or "总共" in q) and ("记录" in q or "多少" in q):
        return "一共有 %d 条记录" % len(data)

    return "暂不支持该问题"


# ------------------------------------------------------------------ main

def main():
    if len(sys.argv) < 3:
        print("用法: python ontology_qa_v3.py <nt文件> '<问题>' [lexicon.json]")
        sys.exit(1)
    nt_file = sys.argv[1]
    question = sys.argv[2]
    lex = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "lexicon.json")
    D = load_dict(lex)
    triples = parse_nt(nt_file)
    data = build_data(triples, D)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)
    print(answer(question, data, D))


if __name__ == "__main__":
    main()
