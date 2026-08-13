#!/usr/bin/env python3
"""multi_model.py — 多文件/多表统一建模桥接（Web 后端调用）

复用 schema_ontology 已验证的 schema-free 多表建模能力：
  load_all(data_dir) → suggest_schema(data) 自动推断 → to_nt 生成本体 .nt
数据本地处理，不出厂（本地局域网场景）。极简：只做桥接，不重写建模逻辑。

用法:
  python multi_model.py <data_dir> [table]
"""
import os
import sys
import json
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
STATE = os.path.join(ROOT, "current.json")


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _build_synonym_map(terms):
    """LLM 语义聚类：把枚举取值 + 属性中文名合并为同义词组 {规范词: [同义词...]}。
    借鉴 DSE-RE 图语义聚类思路解决别名命中（乳制品→奶制品 / 机加设备→机加工设备）。
    - 一次 LLM 调用，输出 JSON {词: [同义词...]}，同义词尽量来自取值(可含口语别名)。
    - LLM 失败回落：返回 {}（不阻塞建模，保持原枚举收集）。
    """
    terms = sorted({t.strip() for t in terms if t and len(t.strip()) >= 2})
    if not terms:
        return {}
    try:
        from model_llm import llm_generate
        import re as _re
        prompt = (
            "下面是某工厂/企业数据里的枚举取值(类型/状态/区域等)，每一个都指代一个独立概念，"
            "例如\"乳制品\"表示一个产品类别，\"机加设备\"表示一种设备类型。\n"
            f"取值: {terms}\n"
            "有些取值存在同义词/别名/习惯叫法（口语、简称、不同写法、行业习惯称呼），"
            "例如\"乳制品\"→[\"乳制品\",\"奶制品\",\"酸奶类\",\"乳品\"]，\"机加设备\"→[\"机加设备\",\"机加工设备\",\"机械加工设备\"]。\n"
            "请只对**确实有同义/别名**的取值生成同义词组，输出 JSON 对象: "
            "{\"规范词\": [\"同义词1\",\"同义词2\",...], ...}。\n"
            "严格要求：\n"
            "1. 规范词必须取自上面的取值列表；\n"
            "2. 同义词/别名是**同一个概念**的其它叫法，可含不在列表里的口语词；\n"
            "3. 不要把不同概念归成一组（如不要把\"乳制品/果汁饮品/烘焙食品\"当同义词，它们是不同类别）；\n"
            "4. 没有同义词的取值不要输出。\n"
            "只输出 JSON，不要任何解释。"
        )
        raw = llm_generate(prompt, temperature=0.2, max_tokens=1500)
    except Exception:
        return {}
    if not raw or raw.startswith("[模型错误]") or raw.startswith("[模型调用失败]"):
        return {}
    m = _re.search(r"\{.*\}", raw, _re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    smap = {}
    for canon, syns in obj.items():
        if not isinstance(syns, list):
            continue
        canon = str(canon).strip()
        cleaned = []
        for s in syns:
            s = str(s).strip()
            if s and s != canon and s not in cleaned:
                cleaned.append(s)
        if canon and cleaned:
            smap[canon] = cleaned
    return smap


def _has_cjk(s) -> bool:
    """是否含中文字符（用于判断中文 label）。"""
    return bool(s) and any("\u4e00" <= ch <= "\u9fff" for ch in str(s))


# 实体表名词干 -> 中文计数词（跨行业泛化：建库时把任意新行业表名映射为中文实体词）。
# 与 schema_ontology._ENTITY_CN 互补——覆盖 schema-free 表名去域前缀后的通用词干。
_ENTITY_STEM_CN = {
    "equipment": "设备", "product": "产品", "products": "产品",
    "line": "测线", "lines": "测线", "shot": "炮点", "shots": "炮点",
    "project": "项目", "projects": "项目",
    "vessel": "船", "vessels": "船", "order": "订单", "orders": "订单",
    "batch": "批次", "batches": "批次", "batch_ingredient": "批次配料",
    "customer": "客户", "customers": "客户",
    "raw_material": "原料", "raw_materials": "原料", "sale": "销售", "sales": "销售",
    "qc": "质检", "team": "班组", "dock": "船坞", "dock_yard": "船坞",
    "device": "设备", "devices": "设备", "machine": "机器", "machines": "机器",
    "ai4i": "设备", "record": "记录", "records": "记录", "data": "数据",
}

# 数值属性名 -> 中文名（data profiling 自动识别数值列后，给中文极值词）。
# 覆盖高频数值语义：精度/投资/价格/吨位/金额/容量/功率等。
_NUMERIC_ATTR_CN = {
    "accuracy": "精度", "investment": "投资", "investment_wan": "投资",
    "price": "价格", "amount": "金额", "tonnage": "吨位", "tonnage_dwt": "吨位",
    "capacity": "容量", "capacity_t": "容量", "power": "功率", "power_kw": "功率",
    "quantity": "数量", "stock": "库存", "layers": "层数", "progress": "进度",
    "progress_pct": "进度", "contract_amount": "合同金额", "contract_amount_wan": "合同金额",
    "lifting_capacity_t": "起重能力", "shotpoints": "炮点数", "receivers": "检波点数",
    "charge_kg": "药量", "depth_m": "炮点深度", "record_length_s": "记录长度",
}
# 数值关键字（类型推断为 number 之外的兜底识别）
_NUMERIC_KEYWORDS = {
    "accuracy", "investment", "price", "tonnage", "amount", "capacity", "power",
    "quantity", "layers", "progress", "stock", "temperature", "pressure", "depth",
    "weight", "rate", "score", "amount_wan", "cost",
}


def _build_lexicon(schema, data):
    """从 suggest_schema 生成基础词典（attr_cn2en/attr_en2cn/status_cn2en/type_cn2en 等），供 ask 问答使用。
    极简：直接用 suggest_schema 推断出的中文属性 label 生成自然词典
    （生产日期→produce_date / 批次编号→batch_id / 原料→raw_parts），缺失时用英文名兜底。
    - attr_cn2en 额外生成去掉单位的基名别名（功率(kW)→功率），兼容用户口语（功率最大的设备）。
    - type_cn2en 从类型列（device_type/type/category）的实际值生成：设备类型值→中文。
    - status_cn2en 优先用数据里的真实枚举值（中文值直接映射自身），英文运维词兜底。
    - entity_cn2en：data profiling 自动生成 {中文实体名: 表名}，替代实体总数硬编码。
    - numeric_fields：data profiling 识别数值列 -> {中文极值词: 英文字段}，替代极值硬编码。"""
    import re as _re
    cn = {}  # 中文名 -> 英文名
    en = {}  # 英文名 -> 中文名
    type_vals = {}   # 类型列取值 -> 取值（值已是中文）
    status_vals = {} # 状态列取值 -> 取值
    zone_vals = {}   # 区域列取值 -> 取值
    # 第一遍：收集类型/状态/区域列取值（每个实体都处理，不做去重跳过，
    # 否则同名列(如 status)在第一个实体后被 `name in en` 跳过，漏收后续实体取值）
    for e in schema.get("entities", []):
        table = e.get("table")
        rows = (data or {}).get(table) or []
        for a in e.get("attributes", []):
            name = a["name"]
            lname = name.lower()
            label = a.get("label") or name
            if "type" in lname or "类型" in lname or lname in ("category", "kind"):
                for r in rows:
                    v = r.get(name)
                    if v is not None and str(v).strip():
                        sv = str(v).strip()
                        type_vals[sv] = sv
            elif lname in ("status", "state", "result", "qc_result") \
                    or (a.get("type") == "enum" and "type" not in lname):
                for r in rows:
                    v = r.get(name)
                    if v is not None and str(v).strip():
                        sv = str(v).strip()
                        status_vals[sv] = sv
            elif lname in ("region", "location", "zone", "area", "workshop") \
                    or "区域" in label or "车间" in label:
                for r in rows:
                    v = r.get(name)
                    if v is not None and str(v).strip():
                        sv = str(v).strip()
                        zone_vals[sv] = sv
    # 第二遍：构建属性名中英文词典（跨实体去重）
    for e in schema.get("entities", []):
        for a in e.get("attributes", []):
            name = a["name"]
            if name in en:
                continue
            label = a.get("label") or name   # 中文 label（LLM/规则），缺失则英文兜底
            cn[label] = name
            en[name] = label
            # 基名别名：去掉单位括号（功率(kW)→功率），兼容口语提问
            base = _re.sub(r'[（(][^）)]*[）)]', '', label).strip()
            if base and base != label and base not in cn:
                cn[base] = name
    status_cn2en = {"运行中": "running", "待机": "idle", "报警": "alarm",
                    "维护中": "maintenance", "离线": "offline",
                    "合格": "pass", "不合格": "fail"}
    status_cn2en.update(status_vals)  # 真实数据枚举优先（中文值直接映射自身）
    # LLM 语义聚类合并同义词（解决别名命中，如 乳制品→奶制品 / 机加设备→机加工设备）。
    # 失败回落返回 {}，不阻塞建模。词源：类型/状态/区域枚举值 + 属性中文名。
    _terms = list(type_vals) + list(status_vals) + list(zone_vals) + list(cn)
    synonym_map = _build_synonym_map(_terms)

    # ── data profiling: 实体总数映射 {中文实体名: 表名}（替代硬编码 {设备:equipment,...}）──
    # 计数词双来源：① 表名词干→中文（projects→项目/船/vessels→船，用户常问的词，稳定）；
    #               ② schema 中文 label（Valve_batches→批次，或 LLM 增强的 环保项目/船舶）。
    # 两者都注册为计数词，保证"有多少个项目/有多少艘船"等口语命中。
    entity_cn2en = {}
    for e in schema.get("entities", []):
        table = e.get("table")
        if not table:
            continue
        label = e.get("label") or ""
        # 表名去域前缀：<domain>_<stem>（valve_batches→batches / seis_lines→lines）
        stem = table.split("_", 1)[1] if "_" in table else table
        stem_word = (_ENTITY_STEM_CN.get(stem) or _ENTITY_STEM_CN.get(stem.split("_")[-1]) or "")
        words = []
        if stem_word:
            words.append(stem_word)
        if _has_cjk(label) and label != stem_word:
            words.append(label)
        for w in words:
            entity_cn2en.setdefault(w, table)

    # ── data profiling: 极值字段 {中文极值词: 英文字段}（替代极值硬编码）──
    def _numeric_cn(snake):
        """数值字段名 -> 中文极值词。优先精确匹配，其次前缀匹配（accuracy_mm→精度）。"""
        if snake in _NUMERIC_ATTR_CN:
            return _NUMERIC_ATTR_CN[snake]
        for k in sorted(_NUMERIC_ATTR_CN, key=len, reverse=True):
            if snake.startswith(k + "_"):
                return _NUMERIC_ATTR_CN[k]
        return _NUMERIC_ATTR_CN.get(snake.split("_")[-1], "")

    numeric_fields = {}
    for e in schema.get("entities", []):
        table = e.get("table")
        for a in e.get("attributes", []):
            name = a.get("name") or ""
            if not name:
                continue
            ptype = a.get("type") or ""
            label = a.get("label") or name
            snake = _re.sub(r'([a-z])([A-Z])', r'\1_\2', name).lower()
            last = snake.split("_")[-1]
            # 数值列识别：类型推断为 number，或字段名含数值语义关键字
            is_num = ptype == "number" or last in _NUMERIC_KEYWORDS \
                or snake in _NUMERIC_ATTR_CN or last in _NUMERIC_ATTR_CN \
                or any(snake.startswith(k + "_") for k in _NUMERIC_ATTR_CN)
            if not is_num:
                continue
            base = _re.sub(r'[（(][^）)]*[）)]', '', label).strip()
            # 优先用显式数值语义映射（合同金额/投资/吨位…），避免 label 混拼(contract金额wan)；
            # 否则用去单位的中文 label（功率(kW)→功率）。
            cname = _numeric_cn(snake) or (base if _has_cjk(base) else "")
            if cname:
                numeric_fields.setdefault(cname, name)

    return {
        "description": "自动生成词典（multi_model, suggest_schema 推断 + LLM 语义聚类同义词 + data profiling 映射）",
        "attr_cn2en": cn, "attr_en2cn": en,
        "status_cn2en": status_cn2en,
        "type_cn2en": type_vals,
        "zone_cn2en": zone_vals,
        "synonym_map": synonym_map,
        "entity_cn2en": entity_cn2en,          # {中文实体名: 表名} 实体总数映射
        "numeric_fields": numeric_fields,      # {中文极值词: 英文字段}
        "field_aliases": {"status": ["status"], "deviceType": ["deviceType", "device_type", "type"], "deviceName": ["deviceName", "device_name", "name"]},
        "value_fields": [],
    }


def build_data(data, table="factory_multi"):
    """对内存中的 {表名: [行...]} 直接建模（DB 直连场景：省去 CSV 落盘中间步）。

    与 build() 共用同一套已验收建模流水线（suggest_schema + to_nt + 词典 + state），
    供 db_to_ontology 一键从 ERP/MES 数据库建本体复用。返回 (table, tables列表, nt行数)。
    """
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    if not data:
        raise ValueError("[建模失败] 无可建模的数据表（未从数据库读到任何行）")
    schema = so.suggest_schema(data)           # 自动推断 schema（schema-free）
    os.makedirs(OUT, exist_ok=True)
    nt = os.path.join(OUT, f"{table}.nt")
    lines = so.to_nt(data, schema, outpath=nt)  # 写 N-Triples
    if not os.path.exists(nt):
        raise RuntimeError("to_nt 未生成本体文件")
    # 生成基础词典（供 ask 问答），写入 config/
    lexicon = _build_lexicon(schema, data)
    lex_path = os.path.join(ROOT, "config", f"lexicon_{table}.json")
    with open(lex_path, "w", encoding="utf-8") as f:
        json.dump(lexicon, f, ensure_ascii=False, indent=2)
    json.dump({"nt": os.path.relpath(nt, ROOT), "table": table,
               "data_dir": None,
               "source": "db",
               "lexicon": os.path.relpath(lex_path, ROOT)},
              open(STATE, "w", encoding="utf-8"))
    return table, list(data.keys()), len(lines)


def build(data_dir, table="factory_multi"):
    """load_all + suggest_schema + to_nt 统一建模。返回 (table, tables列表, nt行数)。"""
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    data = so.load_all(data_dir)               # {表名: [行...]}
    return build_data(data, table)


def main():
    if len(sys.argv) < 2:
        print("用法: python multi_model.py <data_dir> [table]"); sys.exit(1)
    data_dir, table = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "factory_multi")
    try:
        table, tables, n = build(data_dir, table)
        print(f"✅ 多表建模完成: {len(tables)} 表 -> {table}.nt ({n} 行 N-Triples)")
        print(f"   表: {tables}")
    except Exception as e:
        print(f"❌ 多表建模失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
