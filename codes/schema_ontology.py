#!/usr/bin/env python3
"""schema_ontology.py — 工厂本体 schema 驱动建模（移植 sme-decision-ontology 精髓）

在现有 N-Triples 自动建模之外，新增一条**企业级 schema 驱动**建模路径：
显式声明实体/属性/关系/约束 → 跨表建统一实例图 → 约束校验 → 跨域图遍历。

融合 sme-decision-ontology `core/ontology.py` 的本体重构精髓：
1. schema 驱动（ontology.json 显式声明，非纯 CSV 推断）
2. 属性语义角色（Palantir Property: identifier/reference/measure/category/timestamp/text）
3. 类型体系（Enterprise → BusinessObject → 业务域类 → 实体）
4. 跨表 join 建统一实例图（build_graph，双向 FK）
5. 约束校验（validate: unique/required/positive）
6. 跨域图遍历（traverse）

与现有 N-Triples 兼容并存：本模块产出 Python dict 图（内存），不替代 csv_to_owl/multi_table 的 RDF 输出。
可独立使用，也可作为 run.py 的可选增强建模层。

用法:
  from schema_ontology import load_schema, build_graph, validate, traverse, build_ontology_model
  schema = load_schema("config/ontology_schema.json")
  data = load_all(data_dir)               # {表名: [行...]}
  graph = build_graph(data, schema)
  issues = validate(data, schema)
  model = build_ontology_model(data, schema)
"""
import os
import json
from collections import Counter

# ═══════════ 数据加载（复用 factory data_loader 接口）═══════════
def load_all(data_dir: str) -> dict:
    """加载数据目录下所有 CSV/JSON/SQLite/Excel 表 → {表名: [行...]}（动态发现）。

    失败时报告清晰原因：目录不存在 / 目录为空 / 无支持格式数据，而非裸抛异常。
    """
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"[建模失败] 数据目录不存在: {data_dir}")
    from data_loader import load_table
    data = {}
    found = 0
    for f in sorted(os.listdir(data_dir)):
        if not f.startswith(".") and os.path.splitext(f)[1].lower() in (".csv", ".json", ".db", ".sqlite", ".sqlite3", ".xlsx", ".xls"):
            found += 1
            try:
                name, _headers, rows = load_table(os.path.join(data_dir, f))
            except Exception as e:
                raise ValueError(f"[建模失败] 加载表 {f} 出错: {e}")
            if rows:
                data[name] = rows
    if found == 0:
        raise ValueError(f"[建模失败] 数据目录 {data_dir} 下无 CSV/JSON/SQLite/Excel 数据文件")
    if not data:
        raise ValueError(f"[建模失败] 数据目录 {data_dir} 下文件均为空，未加载到任何数据行")
    return data


# ═══════════ schema 加载与校验 ═══════════
def load_schema(path: str) -> dict:
    """加载 + 校验本体 schema（实体/关系/约束合法性）。

    失败时报告清晰原因：文件不存在 / JSON 非法 / 实体冲突 / 关系引用不存在。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"[建模失败] schema 文件不存在: {path}")
    try:
        schema = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"[建模失败] schema 文件 {path} 不是合法 JSON: {e}")
    entities = {e["id"]: e for e in schema.get("entities", [])}
    if not entities:
        raise ValueError(f"[建模失败] schema {path} 未定义任何实体(entities)")
    # 实体 id 唯一（assert → 显式异常，报告具体重复项）
    dup = [eid for eid, c in Counter(
        e["id"] for e in schema.get("entities", [])).items() if c > 1]
    if dup:
        raise ValueError(f"[建模失败] schema 实体 id 重复: {dup}")
    # 关系 from/to 必须存在
    for r in schema.get("relations", []):
        if r["from"] not in entities:
            raise ValueError(f"[建模失败] 关系 {r['id']} 的 from={r['from']} 不存在于实体")
        if r["to"] not in entities:
            raise ValueError(f"[建模失败] 关系 {r['id']} 的 to={r['to']} 不存在于实体")
    schema["_entities"] = entities
    return schema


# ═══════════ 属性语义角色（Palantir Property）═══════════
_REF_HINTS = ("_id", "_code", "_no", "id")
_MEASURE_HINTS = ("qty", "amount", "price", "cost", "stock", "pct", "days", "months", "age", "limit", "rank", "rate", "num", "weight", "size", "power", "kw")
_DATE_HINTS = ("date", "time", "day", "install", "create", "timestamp")
_CATEGORY_HINTS = ("category", "type", "status", "state", "kind", "flag", "grade", "level")


def _infer_prop_role(col: str, ptype: str) -> str:
    """属性语义角色分类(identifier/reference/measure/category/timestamp/text)。

    顺序关键: reference(*_id/*_code) 优先于 identifier(恰好是 id)——
    product_id 是引用列不是主键, 只有严格等于 'id' 才是 identifier。
    """
    low = col.lower()
    if col.endswith("_id") or col.endswith("_code"):
        return "reference"
    if low == "id":
        return "identifier"
    if ptype == "number" or any(h in low for h in _MEASURE_HINTS):
        return "measure"
    if ptype == "date" or any(h in low for h in _CATEGORY_HINTS):
        return "category"
    if any(h in low for h in _DATE_HINTS):
        return "timestamp"
    return "text"


def classify_properties(schema: dict) -> dict:
    """为每个实体的属性标注语义角色。"""
    for e in schema.get("entities", []):
        for a in e.get("attributes", []):
            a["role"] = _infer_prop_role(a["name"], a.get("type", "string"))
        for a in e.get("attributes", []):
            if a["name"] == e.get("key"):
                a["role"] = "identifier"
    return schema


# ═══════════ 类型体系（Type hierarchy）═══════════
def build_class_hierarchy(schema: dict) -> list:
    """类型体系：Enterprise → BusinessObject → 业务域类 → 实体（Is-A）。"""
    hierarchy = [
        {"name": "Enterprise", "super": None, "label": "企业"},
        {"name": "BusinessObject", "super": "Enterprise", "label": "业务对象"},
    ]
    domains = {}
    for e in schema.get("entities", []):
        d = e.get("domain", "其他域")
        domains.setdefault(d, {"name": d, "super": "BusinessObject", "label": d, "children": []})
        domains[d]["children"].append(e["id"])
    for d in domains.values():
        hierarchy.append({"name": d["name"], "super": "BusinessObject", "label": d["label"], "entities": d["children"]})
    for e in schema.get("entities", []):
        if "category" in [a["name"] for a in e.get("attributes", [])]:
            hierarchy.append({"name": f"{e['id']}Category", "super": e["id"], "label": f"{e['label']}类别", "kind": "is_a"})
    return hierarchy


def enrich_links(schema: dict) -> dict:
    """链接类型：关系补 inverse 反向标签 + kind(data/derived)。"""
    for r in schema.get("relations", []):
        r["kind"] = "derived" if r.get("abstract") else "data"
        r["inverse"] = {"Product→Supplier": "供应商提供产品", "Product→Inventory": "产品库存",
                        "Product→Sale": "产品销售", "Sale→Customer": "客户购买",
                        "Purchase→Supplier": "供应商供货", "Purchase→Product": "产品被采购",
                        "Production→Product": "产品被生产", "Production→Equipment": "设备被使用",
                        "Payment→Customer": "客户付款"}.get(f"{r['from']}→{r['to']}", "关联")
    return schema


def build_ontology_model(data: dict, schema: dict) -> dict:
    """构建 Palantir 风格企业本体模型：对象类型 + 链接类型 + 类型体系 + 语义域。"""
    schema = classify_properties(schema)
    schema = enrich_links(schema)
    return {
        "object_types": schema["entities"],
        "link_types": schema["relations"],
        "type_hierarchy": build_class_hierarchy(schema),
        "semantic_domains": sorted({e.get("domain", "其他域") for e in schema["entities"]}),
        "instance_counts": {e["id"]: len(data.get(e.get("table", ""), [])) for e in schema["entities"]},
    }


# ═══════════ 外键推断 + 跨表建图 ═══════════
# 关系中文 label（按 FK 语义词映射，避免 auto_ 英文前缀）
_REL_CN = {
    "product": "生产产品", "raw": "使用原料", "batch": "所属批次",
    "customer": "售予客户", "equipment": "使用设备", "supplier": "采购自供应商",
    "material": "使用原料",
}


def _singular(w: str) -> str:
    """极简英文单词单数化（词干匹配用）：products→product / batches→batch / materials→material。"""
    w = str(w).lower()
    if w.endswith("ies") and len(w) > 3:
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith(("ches", "shes", "xes", "zes", "ses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 1:
        return w[:-1]
    return w


def _match_target(raw: str, table: str, data: dict):
    """FK 目标实体匹配：单复数词干匹配表名语义词 + 排除自身。每 FK 列至多一个目标。

    参考 sme modeling.suggest_schema 的 `_match_target`（词干匹配 + 排除自身）。
    同词干多候选时优先主键为 id 的顶层实体，避免 FK 指向明细/关联表自身
    （如 valve_batch_ingredient.batch_id → Valve_batches 而非自身）。
    """
    raw_s = _singular(raw)
    cand = []
    for tname, rows in data.items():
        if tname == table or not rows:
            continue
        toks = [_singular(p) for p in tname.lower().split("_") if p]
        if raw_s in toks or raw.lower() in toks:
            cand.append(tname)
    if not cand:
        return None

    def _score(t):
        toks = [_singular(p) for p in t.lower().split("_") if p]
        exact = 0 if (raw_s in toks or raw.lower() in toks) else 1
        has_id = 0 if "id" in (data[t][0].keys() if data[t] else []) else 1
        return (exact, has_id)

    return _cap(min(cand, key=_score))


def _infer_relations(data: dict) -> list:
    """关系发现（自动）：不只显式外键，还发现语义关联，增密关系链。

    分三层发现（确定性、零依赖、可解释）：
    1. 显式外键：`*_id`/`*_code`/`*_key` 列 → N:1（原逻辑保留）
    2. 隐式外键：非 `_id` 后缀但列名词干命中某表实体（如 owner_team→teams 表）→ N:1
    3. 同域值域重叠：两表共享维度列（列名+值域高度重叠，如 region 在 lines/teams）→ 关联

    关系 id 用 {表}_{列}，label 用中文（product_id→生产产品）。
    """
    inferred = []
    seen_ids = set()
    tables = [t for t in data if data.get(t)]
    _sample = lambda t: (data[t][0] if data[t] else {})

    # 1. 显式外键（原逻辑）
    for table in tables:
        sample = _sample(table)
        for col in sample:
            if not (col.endswith("_id") or col.endswith("_code") or col.endswith("_key")):
                continue
            raw = col.replace("_id", "").replace("_code", "").replace("_key", "")
            target = _match_target(raw, table, data)
            if target is None:
                continue
            label = _REL_CN.get(raw) or _REL_CN.get(_singular(raw)) or "关联" + _entity_cn_label(target)
            rid = f"{table}_{col}"
            inferred.append({
                "id": rid, "from": _cap(table), "to": target,
                "fk": f"{table}.{col}", "cardinality": "N:1", "label": label, "auto": True,
                "source": "fk",
            })
            seen_ids.add(rid)

    # 2. 隐式外键：非 `_id` 后缀但词干命中表实体（owner_team→teams, workshop→?）
    #   避免与显式外键重复；排除 id/name/status/type 等通用列(易误连)
    _GENERIC_COLS = {"id", "name", "status", "type", "category", "created_at", "updated_at",
                     "timestamp", "date", "time", "remark", "note", "description", "comment"}
    for table in tables:
        sample = _sample(table)
        for col in sample:
            if col.endswith(("_id", "_code", "_key")) or col in _GENERIC_COLS:
                continue
            target = _match_target(col, table, data)
            if target is None or target == _cap(table):
                continue
            # 确认列值域与目标实体主键值有重叠(隐式外键判定), 否则跳过
            vals = {str(r.get(col, "")).strip() for r in data[table] if r.get(col)}
            pk_col = _primary_key_col(data[target])
            if not pk_col or not vals:
                continue
            target_vals = {str(r.get(pk_col, "")).strip() for r in data[target]}
            overlap = vals & target_vals
            if not overlap:
                continue
            rid = f"{table}_{col}"
            if rid in seen_ids:
                continue
            label = _REL_CN.get(col) or _REL_CN.get(_singular(col)) or "关联" + _entity_cn_label(target)
            inferred.append({
                "id": rid, "from": _cap(table), "to": target,
                "fk": f"{table}.{col}", "cardinality": "N:1", "label": label, "auto": True,
                "source": "implicit_fk",
            })
            seen_ids.add(rid)

    # 3. 同域值域重叠：两表共享维度列(列名相同或词干相同 + 值域重叠≥阈值) → 关联
    _col_key = lambda c: c.lower().replace("_", "").replace(" ", "")
    for i, ta in enumerate(tables):
        sa = _sample(ta)
        for tb in tables[i + 1:]:
            sb = _sample(tb)
            for ca in sa:
                for cb in sb:
                    if ca == cb and _col_key(ca) in {"region", "zone", "area", "location", "workshop",
                                                     "plant", "site", "district", "position", "place"}:
                        # 共享空间/位置维度列 → 同域关联
                        vals_a = {str(r.get(ca, "")).strip() for r in data[ta] if r.get(ca)}
                        vals_b = {str(r.get(cb, "")).strip() for r in data[tb] if r.get(cb)}
                        if vals_a and vals_b and len(vals_a & vals_b) >= 1:
                            rid = f"{ta}_{tb}_{ca}"
                            if rid in seen_ids:
                                continue
                            inferred.append({
                                "id": rid, "from": _cap(ta), "to": _cap(tb),
                                "fk": f"{ta}.{ca}={tb}.{cb}", "cardinality": "N:M",
                                "label": f"同属{_cn_dim(ca)}", "auto": True, "source": "shared_dim",
                            })
                            seen_ids.add(rid)
    return inferred


def _primary_key_col(table_rows: list) -> str:
    """找表的主键列：优先 id/xx_id，否则首列。"""
    if not table_rows:
        return ""
    cols = list(table_rows[0].keys())
    for c in cols:
        if c == "id" or c.endswith("_id"):
            return c
    return cols[0] if cols else ""


def _cn_dim(col: str) -> str:
    """维度列中文名(共享空间/位置列的简单映射)。"""
    return {
        "region": "区域", "zone": "区域", "area": "区域", "location": "位置",
        "workshop": "车间", "plant": "厂区", "site": "站点", "district": "区",
    }.get(col.lower(), col)


def _infer_relations_llm(data: dict, rules_rels: list) -> list:
    """LLM 兜底关系发现：规则关系稀疏时，用远端大模型分析表结构识别语义关系。

    规则发现不到、但表间存在明显语义关联时（跨表共享语义、业务关系），用 LLM 补。
    离线/无 key/调用失败 → 静默返回 []（不阻断建模，保持确定性优先）。
    仅当规则关系数 < 表数时触发（稀疏才兜底），避免过度依赖模型。
    """
    tables = [t for t in data if data.get(t)]
    if len(tables) < 2 or len(rules_rels) >= len(tables):
        return []  # 不稀疏则不兜底
    try:
        import model_llm as ml
        # 构造表结构摘要
        summary_lines = []
        for t in tables:
            cols = list(data[t][0].keys()) if data[t] else []
            summary_lines.append(f"{t}({'/'.join(cols)})")
        prompt = (
            "你是企业本体建模专家。下面是一个工厂的多个数据表（表名(列名)）。\n"
            + "\n".join(summary_lines) +
            "\n\n请识别表之间的**语义关系**（不只显式外键，包括同域、业务关联、主从关系）。"
            "只输出 JSON 数组，每项 {from, to, label(中文关系名), reason}，from/to 用表名。"
            "若无额外关系输出 []。"
        )
        raw = ml.llm_generate(prompt, temperature=0.2, max_tokens=400)
        if not raw or raw.startswith("[模型不可用]"):
            return []
        import re, json as _json
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            return []
        items = _json.loads(m.group(0))
        out = []
        for it in items:
            frm, to = str(it.get("from", "")), str(it.get("to", ""))
            if frm and to and frm != to:
                # 规范化表名为实体(首字母大写)
                out.append({
                    "id": f"llm_{frm}_{to}", "from": _cap(frm), "to": _cap(to),
                    "fk": "", "cardinality": "N:M", "label": str(it.get("label", "语义关联")),
                    "auto": True, "source": "llm", "reason": str(it.get("reason", "")),
                })
        # 去重(过滤与规则已发现的重复)
        seen = {(r["from"], r["to"]) for r in rules_rels}
        return [r for r in out if (r["from"], r["to"]) not in seen]
    except Exception:
        return []


def _cap(name: str) -> str:
    return name[0].upper() + name[1:] if name else name


# ═══════════ schema 自动推断（schema-free 范式，无手写 ontology_schema.json）═══════════
def _guess_col_type(values) -> str:
    """从样本值推断属性类型：number / date / string（极简启发式）。"""
    seen_num = seen_date = 0
    for v in values:
        if v is None or str(v).strip() == "":
            continue
        s = str(v).strip()
        try:
            float(s)
            seen_num += 1
            continue
        except (TypeError, ValueError):
            pass
        # 松散日期启发（YYYY-MM-DD / YYYY/MM/DD）
        if len(s) >= 8 and (s[4] in "-/" and s[7] in "-/"):
            seen_date += 1
            continue
    if seen_date > seen_num:
        return "date"
    if seen_num:
        return "number"
    return "string"


# ═══════════ 中文 label 规则映射（LLM 失败回落，确定性兜底）═══════════
_ENTITY_CN = {
    "batch": "批次", "batches": "批次", "batch_ingredient": "批次配料",
    "product": "产品", "products": "产品", "customer": "客户", "customers": "客户",
    "equipment": "设备", "raw_material": "原料", "raw_materials": "原料",
    "sale": "销售", "sales": "销售", "qc": "质检", "qc_check": "质检",
}
_ATTR_CN = {
    "id": "编号", "name": "名称", "type": "类型", "status": "状态",
    "product_name": "产品名称", "device_name": "设备名称", "customer_name": "客户名称",
    "device_type": "设备类型", "model_code": "型号代码", "part_name": "部件名称",
    "produce_date": "生产日期", "check_date": "检查日期", "sale_date": "销售日期",
    "batch_id": "批次编号", "product_id": "产品编号", "raw_id": "原料编号", "customer_id": "客户编号",
    "raw_parts": "原料", "material": "材质", "supplier": "供应商", "region": "区域", "industry": "行业",
    "credit_level": "信用等级", "workshop": "车间", "power_kw": "功率(kW)",
    "pressure_grade": "压力等级", "connection": "连接方式", "seal_material": "密封材质",
    "body_material": "阀体材质", "standard_no": "标准号", "temp_range": "温度范围",
    "quantity": "数量", "amount": "金额", "price": "价格", "stock": "库存",
    "check_item": "检查项目", "press_rule": "压力规则", "hold_sec": "保压秒数",
    "leak_bubbles_min": "泄漏气泡", "result": "结果", "checker": "检查员",
    "team": "班组", "qc_result": "质检结果", "vibration_mm_s": "振动(mm/s)",
    "temp_c": "温度(℃)", "current_a": "电流(A)", "size_mm": "尺寸(mm)",
    # AI4I 预测性维护列（中文 label 兜底, 覆盖英文列名防中英混杂）
    "air_temperature": "空气温度", "air_temperature_k": "空气温度(K)",
    "process_temperature": "工艺温度", "process_temperature_k": "工艺温度(K)",
    "rotational_speed": "转速", "rotational_speed_rpm": "转速(rpm)",
    "torque": "扭矩", "torque_nm": "扭矩(Nm)",
    "tool_wear": "刀具磨损", "tool_wear_min": "刀具磨损(min)",
    "machine_failure": "机器故障", "twf": "刀具磨损故障", "hdf": "热耗散故障",
    "pwf": "功率故障", "osf": "过冲故障", "rnf": "随机故障",
    # 逐词兜底(拆词查询用): air_temperature_ → air + temperature
    "air": "空气", "temperature": "温度", "process": "工艺",
    "rotational": "转速", "speed": "转速", "wear": "磨损", "tool": "刀具",
}


def _looks_english(s: str) -> bool:
    """是否英文/无中文（用于判断 label 是否缺中文名）。"""
    return bool(s) and not any("\u4e00" <= ch <= "\u9fff" for ch in str(s))


def _entity_cn_label(name: str) -> str:
    """实体中文 label（规则兜底）：表名/实体id → 中文名。Valve_batches→批次、Valve_equipment→设备。"""
    core = str(name).lower()
    for pref in ("valve_", "factory_", "t_", "tb_"):
        if core.startswith(pref):
            core = core[len(pref):]
            break
    if core in _ENTITY_CN:
        return _ENTITY_CN[core]
    last = core.split("_")[-1]
    return _ENTITY_CN.get(last, last)


def _attr_cn_label(name: str) -> str:
    """属性中文 label（规则兜底）：produce_date→生产日期、raw_parts→原料、batch_id→批次编号。"""
    if name in _ATTR_CN:
        return _ATTR_CN[name]
    for suf in ("_id", "_code", "_key"):
        if name.endswith(suf):
            base = name[:-len(suf)]
            return (_ATTR_CN.get(base, base) if base else "编号") + "编号"
    words = [w for w in str(name).replace("-", "_").lower().split("_") if w]
    return "".join(_ATTR_CN.get(w, w) for w in words) if words else name


def llm_enhance(schema: dict, use_llm: bool = True) -> dict:
    """LLM 中文 label 增强（实体 + 属性）。

    参考 sme modeling.llm_enhance：规则引擎兜底（确定性中文名）+ LLM 可选精修，
    失败/无 key/断网一律回落规则，不阻塞建模。label 写入 schema 实体/属性，
    to_nt 用中文 RDFS label 展示。
    """
    # 1) 规则兜底：保证所有实体/属性有中文 label（零 token，确定性）
    for e in schema.get("entities", []):
        if not e.get("label") or e["label"] == e["id"] or _looks_english(e["label"]):
            e["label"] = _entity_cn_label(e.get("table") or e["id"])
        for a in e.get("attributes", []):
            if not a.get("label") or a["label"] == a["name"] or _looks_english(a["label"]):
                a["label"] = _attr_cn_label(a["name"])
    if not use_llm:
        return schema
    # 2) LLM 精修（可选）：无 key 直接回落（规则 label 已够）
    try:
        from model_llm import llm_generate, get_model_config
        cfg = get_model_config()
        if cfg.get("type") == "openai" and not cfg.get("api_key"):
            if not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZHIPU_API_KEY")):
                return schema
        need = [e["id"] for e in schema["entities"] if e["label"] == _entity_cn_label(e.get("table") or e["id"])]
        if not need:
            return schema
        prompt = ("为下列工厂实体生成简短准确的中文名，仅输出JSON {\"id\":\"中文名\"}，不要多余文字：\n"
                  + json.dumps(need, ensure_ascii=False))
        text = llm_generate(prompt, temperature=0.1, max_tokens=300)
        if "{" in text:
            labels = json.loads(text[text.find("{"):text.rfind("}") + 1])
            for e in schema["entities"]:
                if e["id"] in labels and labels[e["id"]]:
                    e["label"] = labels[e["id"]]
    except Exception:
        pass
    return schema


def suggest_schema(data: dict) -> dict:
    """从多表数据自动推断 schema（schema-free，无需手写 ontology_schema.json）。

    遍历 {表名: [行...]}，每表建一个实体（id=表名首字母大写，key=id 或 *_id 或首列，
    attributes=行字段名 + 自动推断类型，复用 _infer_prop_role 标注语义角色）；
    复用 _infer_relations 推断跨表关系；每表主键生成 unique 约束。
    返回可直接喂给 build_graph / validate / to_nt 的 schema dict。
    """
    entities = []
    constraints = []
    for table, rows in data.items():
        if not rows:
            continue
        sample = rows[0]
        # key 选择：优先 'id'，其次任意 '*_id/*_code/*_key' 列，最后首列
        key = "id" if "id" in sample else next(
            (c for c in sample if c.endswith(("_id", "_code", "_key"))), list(sample)[0])
        attributes = []
        for col in sample:
            ptype = _guess_col_type([r.get(col) for r in rows])
            attr = {"name": col, "type": ptype, "role": _infer_prop_role(col, ptype)}
            if col == key:
                attr["required"] = True
            attributes.append(attr)
        eid = _cap(table)
        entities.append({"id": eid, "label": eid, "table": table, "key": key, "attributes": attributes})
        constraints.append({"type": "unique", "on": f"{eid}.{key}", "msg": f"{eid} 主键 {key} 唯一"})
    schema = {
        "version": "1.0",
        "name": "auto-inferred-ontology",
        "entities": entities,
        "relations": (_rels := _infer_relations(data)) + _infer_relations_llm(data, _rels),
        "constraints": constraints,
    }
    # 与 load_schema 对齐：注入 {id: entity} 索引，供 build_graph/validate/to_nt 直接消费
    schema["_entities"] = {e["id"]: e for e in entities}
    # 中文 label 增强（规则兜底 + LLM 可选精修），label 供 to_nt RDFS label 中文展示
    schema = llm_enhance(schema, use_llm=True)
    return schema


def build_graph(data: dict, schema: dict) -> dict:
    """跨表跨域建统一实例图：实体实例 + 关系边（FK join）。"""
    graph = {"nodes": {}, "edges": []}
    entities = schema.get("_entities", {})
    declared_fks = {r.get("fk") for r in schema.get("relations", []) if r.get("fk")}
    inferred = [r for r in _infer_relations(data) if r.get("fk") not in declared_fks]
    relations = list(schema.get("relations", [])) + inferred
    node_ids = {}
    for eid, ent in entities.items():
        table = ent["table"]
        if table not in data:
            continue
        key = ent["key"]
        detail = ent.get("detail", False)
        for i, row in enumerate(data[table]):
            kid = row.get(key)
            node_id = f"{eid}:{kid}@{i}" if detail else f"{eid}:{kid}"
            graph["nodes"][node_id] = {"entity": eid, "id": kid, "idx": i, "data": row}
            node_ids.setdefault(eid, []).append(node_id)
    # 类别类层级（实体含 category → 类别节点 + isA 边）
    for eid, ent in entities.items():
        table = ent["table"]
        if table not in data or not data[table] or "category" not in data[table][0]:
            continue
        key = ent["key"]
        detail = ent.get("detail", False)
        for i, row in enumerate(data[table]):
            cat = row.get("category")
            if not cat:
                continue
            cat_id = f"Category:{eid}:{cat}"
            if cat_id not in graph["nodes"]:
                graph["nodes"][cat_id] = {"entity": "Category", "id": cat, "data": {"name": cat, "of": eid}}
            src = f"{eid}:{row.get(key)}@{i}" if detail else f"{eid}:{row.get(key)}"
            graph["edges"].append({"from": src, "to": cat_id, "rel": "isA", "label": "属于类别"})
    # 边：FK join（支持 FK 在 from 侧或 to 侧）
    for r in relations:
        if r.get("abstract") or not r.get("fk"):
            continue
        ftable, fcol = r["fk"].split(".")
        if ftable not in data:
            continue
        from_e = entities.get(r["from"], {})
        to_e = entities.get(r["to"], {})
        fk_val_to_nodes = {}
        for nd in node_ids.get(r["to"], []):
            info = graph["nodes"][nd]
            fk_val_to_nodes.setdefault(f"{info['id']}", []).append(nd)
        for i, row in enumerate(data[ftable]):
            val = row.get(fcol)
            if not val:
                continue
            # FK 值 = 对侧实体(非 fk 表所在实体)的主键引用
            # fk 在 from 表: 每行 from 实体通过自己的 fk 列连到 to 实体
            # fk 在 to 表: 每行 to 实体通过自己的 fk 列连到 from 实体
            if ftable == from_e.get("table"):
                # fk 在 from 表: src=当前 from 行, dst=to 实体(按 fk 值匹配主键)
                src = f"{r['from']}:{row.get(from_e['key'])}@{i}" if from_e.get("detail") else f"{r['from']}:{row.get(from_e['key'])}"
                for dst in fk_val_to_nodes.get(str(val), []):
                    if src in graph["nodes"]:
                        graph["edges"].append({"from": src, "to": dst, "rel": r["id"], "label": r.get("label", r["id"])})
            elif ftable == to_e.get("table"):
                # fk 在 to 表: src=from 实体(按 fk 值匹配其主键), dst=当前 to 行(用 to 表自身主键)
                src = f"{r['from']}:{val}"
                dst = f"{r['to']}:{row.get(to_e['key'])}@{i}" if to_e.get("detail") else f"{r['to']}:{row.get(to_e['key'])}"
                if src in graph["nodes"] and dst in graph["nodes"]:
                    graph["edges"].append({"from": src, "to": dst, "rel": r["id"], "label": r.get("label", r["id"])})
            else:
                # fk 表既非 from 也非 to: 退化为按 fk 值跨表匹配
                src = f"{r['from']}:{val}"
                for nd in fk_val_to_nodes.get(str(val), []):
                    if src in graph["nodes"]:
                        graph["edges"].append({"from": src, "to": nd, "rel": r["id"], "label": r.get("label", r["id"])})
    return graph


# ═══════════ 约束校验 ═══════════
def validate(data: dict, schema: dict) -> list:
    """约束校验：unique/required/positive + 基数。返回问题清单。"""
    issues = []
    entities = schema.get("_entities", {})
    for ent in entities.values():
        table, key = ent["table"], ent["key"]
        if table not in data:
            continue
        seen = set()
        for row in data[table]:
            kid = row.get(key)
            if not ent.get("detail") and kid in seen:
                issues.append({"severity": "error", "type": "unique", "msg": f"实体 {ent['id']} 主键重复: {kid}"})
            seen.add(kid)
            for attr in ent.get("attributes", []):
                v = row.get(attr["name"])
                if attr.get("required") and (v is None or str(v).strip() == ""):
                    issues.append({"severity": "error", "type": "required", "msg": f"{ent['label']}.{attr['label']} 必填缺失"})
                if attr.get("type") == "number" and v is not None:
                    try:
                        if float(v) < 0 and attr["name"] in ("stock", "price", "cost"):
                            issues.append({"severity": "warn", "type": "positive", "msg": f"{ent['label']}.{attr['label']} 为负: {v}"})
                    except (TypeError, ValueError):
                        pass
    for c in schema.get("constraints", []):
        if c.get("type") == "required":
            ent, attr = c["on"].split(".")
            if ent in entities and entities[ent]["table"] in data:
                for row in data[entities[ent]["table"]]:
                    if not row.get(attr):
                        issues.append({"severity": "error", "type": "required", "msg": c.get("msg", "必填缺失")})
                        break
    return issues


# ═══════════ 跨域图遍历 ═══════════
def traverse(graph: dict, entity: str, eid) -> list:
    """图遍历（跨域）：从某实体实例出发，经关系到达的所有相关实例。"""
    start = f"{entity}:{eid}"
    if start not in graph["nodes"]:
        return []
    result = []
    for e in graph["edges"]:
        if e["from"] == start:
            result.append({"rel": e["rel"], "label": e["label"], "to": e["to"]})
        elif e["to"] == start:
            result.append({"rel": e["rel"], "label": e["label"], "from": e["from"]})
    return result


# ═══════════ 统一 N-Triples 输出（替代 csv_to_owl / multi_table 的建本体职责）═══════════
NS = "http://factory.example/ontology#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATAPROP = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
_NS_URI = "http://www.w3.org/2001/XMLSchema#"

def _guess_type(value) -> str:
    """从实际值推断 xsd 类型（数据驱动）。"""
    v = str(value).strip()
    if v == "":
        return "xsd:string"
    try:
        int(v); return "xsd:integer"
    except ValueError:
        pass
    try:
        float(v); return "xsd:decimal"
    except ValueError:
        pass
    if v.lower() in ("true", "false"):
        return "xsd:boolean"
    return "xsd:string"

def _local_name(col: str) -> str:
    """列名 -> 局部名（去下划线，首词小写后续驼峰）。"""
    parts = [p for p in str(col).replace("-", "_").split("_") if p]
    return parts[0] + "".join(p.capitalize() for p in parts[1:]) if parts else "col"

def _q(v) -> str:
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')

def _nt_class_decls(entities, eid_to_cls, L):
    """类声明：每实体一个 owl:Class + 中文 label。返回 eid->类名映射已由调用方维护。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        L.append(f"<{cls_uri}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{cls_uri}> {RDFS_LABEL} {_q(ent.get('label') or eid_to_cls[eid])} .")


def _nt_property_decls(entities, relations, data, eid_to_cls, L):
    """数据属性 + 对象属性声明（跳过主键/外键列，对象属性用关系英文id）。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in relations if r.get("fk", "").startswith(table + ".")}
        for attr in ent.get("attributes", []):
            aname = attr["name"]
            if aname == key or aname in rel_cols:
                continue
            p = _local_name(aname)
            vals = [r.get(aname) for r in data[table] if r.get(aname)]
            t = _guess_type(vals[0]) if vals else "xsd:string"
            L.append(f"<{NS}{p}> {RDF_TYPE} {OWL_DATAPROP} .")
            L.append(f"<{NS}{p}> {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"<{NS}{p}> {RDFS_RANGE} <{_NS_URI}{t.split(':')[1]}> .")
        for r in relations:
            if r.get("fk", "").startswith(table + "."):
                rel = NS + r["id"]  # 对象属性 URI 用关系 id（下游消费一致）
                L.append(f"<{rel}> {RDF_TYPE} {OWL_OBJPROP} .")
                L.append(f"<{rel}> {RDFS_DOMAIN} <{cls_uri}> .")
                L.append(f"<{rel}> {RDFS_RANGE} <{NS}{eid_to_cls.get(r['to'], r['to'])}> .")
                L.append(f"<{rel}> {RDFS_LABEL} {_q(r.get('label', '关联'))} .")


def _nt_category_hierarchy(entities, data, eid_to_cls, L):
    """类别类层级 + 类型体系(subClassOf)：实体含 category 列 → Category 类 + isA。"""
    for eid, ent in entities.items():
        cls = eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table] or "category" not in data[table][0]:
            continue
        cat_cls = f"{cls}Category"
        L.append(f"<{NS}{cat_cls}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{NS}{cat_cls}> {RDFS_SUBCLASS} <{NS}{cls}> .")
        for row in data[table]:
            cat = row.get("category")
            if cat:
                cat_uri = f"{NS}{cat_cls}_{cat}"
                L.append(f"<{cat_uri}> {RDF_TYPE} {OWL_CLASS} .")
                L.append(f"<{cat_uri}> {RDFS_SUBCLASS} <{NS}{cat_cls}> .")


def _nt_instances(entities, relations, data, eid_to_cls, L):
    """实例 + 数据/对象属性（FK join，明细实体按行建实例）。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        detail = ent.get("detail", False)
        rels_of_table = [r for r in relations if r.get("fk", "").startswith(table + ".")]
        rel_cols = {r["fk"].split(".")[1] for r in rels_of_table}
        seen_ids = set()
        for i, row in enumerate(data[table]):
            kid = row.get(key) or f"{i+1}"
            if str(kid) in seen_ids:
                kid = f"{kid}_{i}"
            seen_ids.add(str(kid))
            inst_uri = f"{cls_uri}_{kid}" + (f"@{i}" if detail else "")
            L.append(f"<{inst_uri}> {RDF_TYPE} <{cls_uri}> .")
            if "category" in row and row.get("category"):
                L.append(f"<{inst_uri}> <{NS}hasCategory> <{NS}{eid_to_cls[eid]}Category_{row['category']}> .")
            for attr in ent.get("attributes", []):
                aname = attr["name"]
                if aname == key or aname in rel_cols:
                    continue
                if aname in row and row.get(aname) is not None and str(row.get(aname)).strip() != "":
                    p = _local_name(aname)
                    t = _guess_type(row[aname])
                    L.append(f"<{inst_uri}> <{NS}{p}> {_q(row[aname])}^^<{_NS_URI}{t.split(':')[1]}> .")
            for r in rels_of_table:
                fcol = r["fk"].split(".")[1]
                if fcol in row and row.get(fcol):
                    rel = NS + r["id"]
                    L.append(f"<{inst_uri}> <{rel}> <{NS}{eid_to_cls.get(r['to'], r['to'])}_{row[fcol]}> .")


def to_nt(data: dict, schema: dict, outpath: str = None) -> list:
    """把 schema 驱动建模结果输出成标准 N-Triples（统一替代 csv_to_owl/multi_table）。

    从 schema(实体/关系/约束) + data(多表) 生成：类声明、数据属性、对象属性、
    实例、类别类层级、类型体系。格式与 csv_to_owl/multi_table 对齐，下游
    ontology_qa_v3.parse_nt / graph_rag.parse_nt 无缝消费。

    outpath 非空则写文件，返回行列表。
    """
    L = []
    entities = schema.get("_entities", schema)
    declared_fks = {r.get("fk") for r in schema.get("relations", []) if r.get("fk")}
    inferred = [r for r in _infer_relations(data) if r.get("fk") not in declared_fks]
    relations = list(schema.get("relations", [])) + inferred
    # 实体 ID -> 类局部名（用表名风格，与 multi_table 下游兼容）
    eid_to_cls = {eid: (ent.get("table", eid).capitalize() if ent.get("table") else eid)
                  for eid, ent in entities.items()}
    _nt_class_decls(entities, eid_to_cls, L)
    _nt_property_decls(entities, relations, data, eid_to_cls, L)
    _nt_category_hierarchy(entities, data, eid_to_cls, L)
    _nt_instances(entities, relations, data, eid_to_cls, L)
    if outpath:
        with open(outpath, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    return L


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.abspath(__file__))
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "data_valve")
    schema_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "config", "ontology_schema.json")
    data = load_all(data_dir)
    print(f"加载数据: {len(data)} 表 -> {list(data.keys())}")
    if os.path.exists(schema_path):
        schema = load_schema(schema_path)
        issues = validate(data, schema)
        print(f"约束校验: {len(issues)} 问题", [i["msg"] for i in issues[:5]])
        graph = build_graph(data, schema)
        print(f"本体图: {len(graph['nodes'])} 节点 / {len(graph['edges'])} 关系边")
        model = build_ontology_model(data, schema)
        print(f"类型体系: {[h['name'] for h in model['type_hierarchy']]}")
        print(f"语义域: {model['semantic_domains']}")
        # 跨域遍历示例：第一个实体类型的第一个实例
        if graph["nodes"]:
            first = next(iter(graph["nodes"].values()))
            rel = traverse(graph, first["entity"], first["id"])
            print(f"遍历 {first['entity']}:{first['id']} -> {[r['label'] for r in rel[:5]]}")
    else:
        print(f"无 schema({schema_path}), 仅加载数据。用 suggest_schema 可自动推断。")
