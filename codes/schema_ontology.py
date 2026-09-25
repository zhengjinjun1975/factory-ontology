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
# build_class_hierarchy 见下方「标准字段层」段落（显式 parent 优先，回退 domain 分组）


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
    # 市场域关系标签兜底(competitor_id/region_id/channel_id/trend_id 等外键词 → 中文)
    "competitor": "与竞品竞争", "region": "售于区域", "channel": "经由渠道",
    "trend": "趋势影响", "market": "市场关联",
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
    """FK 目标实体匹配：单复数词干匹配表名语义词 + 排除自身。每 FK 列至多一个目标。"""
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


def _match_target_implicit(col: str, table: str, data: dict):
    """隐式外键宽松匹配：列名最后一个词干命中目标表名词干（owner_team→teams）。

    比 _match_target 宽松：列名取最后一个 _ 分段词干(owner_team→team, project_id→project),
    与各表名(也取词干)比对, 命中且排除自身/通用列 → 候选; 再用值域重叠校验。
    """
    parts = [p for p in col.lower().split("_") if p]
    if not parts:
        return None
    # 从后往前取词干: owner_team→team, device_name→name(但name是通用列)
    last = _singular(parts[-1]) if parts else ""
    if not last or last in {"id", "name", "code", "key", "type", "no"}:
        return None
    cand = []
    for tname, rows in data.items():
        if tname == table or not rows:
            continue
        toks = [_singular(p) for p in tname.lower().split("_") if p]
        # 目标表名的任一 token 或整体与列尾词干匹配
        if last in toks or last == tname.lower().replace("_", ""):
            cand.append(tname)
    if not cand:
        return None
    # 优先主键为 id 的顶层实体; 返回原始表名(与 data 键一致), 调用方用 _cap 作实体名
    def _score(t):
        has_id = 0 if "id" in (data[t][0].keys() if data[t] else []) else 1
        return has_id
    return min(cand, key=_score)


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
            # 宽松匹配: 列名最后一个词干(owner_team→team) 命中目标表名词干(teams→team)
            target = _match_target_implicit(col, table, data)
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
                "id": rid, "from": _cap(table), "to": _cap(target),
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


def _infer_relations_llm(data: dict, rules_rels: list, model_key: str = None) -> list:
    """LLM 兜底关系发现：规则关系稀疏时，用大模型分析表结构识别语义关系。

    model_key: 'local'(本地ornith)/'cloud'(云端DeepSeek)/None(自动路由+降级)。
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
        # 用 llm_generate_auto(自动路由 local/cloud + 降级), force_key 便于对比本地/云端
        raw, _route = ml.llm_generate_auto(prompt, question="本体关系发现",
                                           temperature=0.2, max_tokens=900, force_key=model_key)
        if not raw or raw.startswith("[模型错误]") or raw.startswith("[模型不可用]"):
            return []
        import re, json as _json
        # 容错解析: 优先匹配完整 JSON 数组; 失败则尝试提取单个 {...} 对象(容忍截断/不完整)
        out = []
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                items = _json.loads(m.group(0))
                for it in items:
                    frm, to = str(it.get("from", "")), str(it.get("to", ""))
                    if frm and to and frm != to:
                        out.append({
                            "id": f"llm_{frm}_{to}", "from": _cap(frm), "to": _cap(to),
                            "fk": "", "cardinality": "N:M", "label": str(it.get("label", "语义关联")),
                            "auto": True, "source": "llm", "reason": str(it.get("reason", "")),
                        })
            except Exception:
                out = []
        if not out:
            # 降级: 逐个匹配 {from,to,label} 对象(容忍截断的不完整JSON)
            for om in re.finditer(r"\{\s*\"from\"\s*:\s*\"([^\"]+)\"\s*,\s*\"to\"\s*:\s*\"([^\"]+)\"([^}]*)\}", raw):
                frm, to = om.group(1), om.group(2)
                label_m = re.search(r"\"label\"\s*:\s*\"([^\"]*)\"", om.group(3))
                label = label_m.group(1) if label_m else "语义关联"
                if frm and to and frm != to:
                    out.append({
                        "id": f"llm_{frm}_{to}", "from": _cap(frm), "to": _cap(to),
                        "fk": "", "cardinality": "N:M", "label": label,
                        "auto": True, "source": "llm",
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
    # 市场域(市场本体, 一处定义三域同读): Region/Competitor/Channel/MarketTrend
    "region": "区域", "regions": "区域",
    "competitor": "竞品", "competitors": "竞品",
    "channel": "渠道", "channels": "渠道",
    "market_trend": "市场趋势", "market_trends": "市场趋势",
    "trend": "市场趋势", "market": "市场", "markettrend": "市场趋势",
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


# 通用业务词表扩充（不限行业；新增行业词往这里加即可，算法自动吃任意前缀）
_ENTITY_CN.update({
    "order": "订单", "orders": "订单", "supplier": "供应商", "suppliers": "供应商",
    "warehouse": "仓库", "inventory": "库存", "shipment": "发货", "delivery": "交付",
    "payment": "付款", "invoice": "发票", "contract": "合同", "project": "项目",
    "employee": "员工", "staff": "员工", "department": "部门", "team": "班组",
    "machine": "机器", "machinery": "机器", "device": "装置", "sensor": "传感器",
    "meter": "仪表", "reading": "读数", "log": "日志", "alert": "告警", "event": "事件",
    "report": "报表", "record": "记录", "category": "类别", "class": "类别",
    "spec": "规格", "specification": "规格", "model": "型号", "brand": "品牌",
    "price": "价格", "cost": "成本", "revenue": "营收", "profit": "利润",
    "defect": "缺陷", "fault": "故障", "inspection": "检验", "test": "试验",
    "standard": "标准", "document": "文档", "drawing": "图纸", "process": "工序",
    "procedure": "流程", "station": "工位", "line": "产线", "workshop": "车间",
    "factory": "工厂", "plant": "厂区", "company": "公司", "enterprise": "企业",
    "region": "区域", "area": "区域", "industry": "行业", "sector": "行业",
    "material": "物料", "materials": "物料", "part": "零件", "parts": "零件",
    "component": "部件", "spare": "备件", "tool": "工装", "mold": "模具",
    "batch": "批次", "lot": "批号", "serial": "序列号", "bom": "物料清单",
    "ingredient": "配料", "recipe": "配方", "formula": "配方",
    "qc": "质检", "quality": "质量", "trace": "溯源", "traceability": "溯源",
    "maintenance": "维护", "repair": "维修", "upkeep": "保养", "energy": "能耗",
    "power": "功率", "consumption": "消耗", "emission": "排放", "safety": "安全",
    "person": "人员", "user": "用户", "account": "账户", "role": "角色",
    "task": "任务", "plan": "计划", "schedule": "排程", "step": "步骤",
})
_ATTR_CN.update({
    "shelf_life": "保质期", "expiry": "有效期", "expire_date": "到期日",
    "produce_date": "生产日期", "production_date": "生产日期", "mfg_date": "生产日期",
    "unit": "单位", "uom": "单位", "remark": "备注", "note": "备注",
    "address": "地址", "phone": "电话", "contact": "联系方式",
    "spec": "规格", "model": "型号", "brand": "品牌",
    "status": "状态", "level": "等级", "grade": "等级",
    "power": "功率", "voltage": "电压", "current": "电流", "temperature": "温度",
    "pressure": "压力", "speed": "转速", "capacity": "产能", "throughput": "产量",
    "workshop": "车间", "line": "产线", "station": "工位", "department": "部门",
    "owner": "负责人", "operator": "操作人", "supplier": "供应商", "customer": "客户",
    "cost": "成本", "price": "价格", "tax": "税", "total": "合计",
    "qty": "数量", "num": "数量", "count": "数量", "number": "数量",
    "duration": "时长", "start": "开始", "end": "结束",
})


def _deplural(w: str) -> str:
    """英文复数归一：materials→material、batches→batch。仅用于查表，不改原值。"""
    return w[:-1] if w.endswith("s") and len(w) > 3 and not w.endswith("ss") else w


def _entity_cn_definition(e: dict) -> str:
    """实体结构性定义（规则兜底，域无关，零 token）。

    据实描述：该实体属于哪个业务域、由哪张表承载、以什么唯一标识、记录哪些关键信息。
    原则：每一句都来自 schema 实件（domain/table/key/attributes），不编造语义。
    这是"结构性定义"，用于满足国标表1 的 Definition 描述项；有语义定义时可覆盖。
    """
    label = e.get("label") or e.get("id") or "实体"
    dom = str(e.get("domain") or "").strip()
    s = f"{dom}中的{label}" if dom else f"业务实体{label}"
    table = e.get("table")
    if table:
        s += f"，数据来源于 {table} 表"
    key = e.get("key")
    if key:
        s += f"，以 {key} 唯一标识"
    attrs = [a.get("label") or a.get("name") for a in (e.get("attributes") or [])]
    attrs = [a for a in attrs if a and a != key][:5]
    if attrs:
        s += f"，记录{'、'.join(attrs)}等信息"
    return s + "。"


def _entity_cn_label(name: str) -> str:
    """实体中文 label（规则兜底，域无关）：任意前缀 + 复数归一 + 片段窗口匹配。

    food_raw_materials→原料、Valve_batch_ingredient→批次配料。
    原则：不假定任何行业前缀（旧版写死 valve_/factory_/t_，food_ 之类就漏掉，
    且取"最后一段"会把 raw_materials 剩成 materials 而丢掉中文）。
    """
    words = [w for w in str(name).replace("-", "_").replace(" ", "_").lower().split("_") if w]
    if not words:
        return name
    # 已含中文(如 h_波纹管) → 剥掉纯英文前缀, 直接留中文部分
    if any("\u4e00" <= ch <= "\u9fff" for ch in str(name)):
        cn = "".join(w for w in words if any("\u4e00" <= ch <= "\u9fff" for ch in w))
        return cn or name

    def _look(chunk: str):
        return _ENTITY_CN.get(chunk) or _ENTITY_CN.get("_".join(_deplural(w) for w in chunk.split("_")))

    # 1) 整名命中（含去复数）
    hit = _look("_".join(words))
    if hit:
        return hit
    # 2) 片段窗口：从长到短；同长度优先靠后的片段 —— 英文表名核心词通常在末尾
    #    (energy_station_devices 应命中 devices→设备，而非 energy→能耗)
    n = len(words)
    for size in range(n, 0, -1):
        for i in range(n - size, -1, -1):
            hit = _look("_".join(words[i:i + size]))
            if hit:
                return hit
    # 3) 逐词拼接（每词查表，未收录保留原词）；拼出中文才算成功, 否则保留原名
    parts = [_ENTITY_CN.get(_deplural(w)) or _ENTITY_CN.get(w) or w for w in words]
    joined = "".join(parts)
    return joined if any("\u4e00" <= ch <= "\u9fff" for ch in joined) else name


def _attr_cn_label(name: str) -> str:
    """属性中文 label（规则兜底）：produce_date→生产日期、raw_parts→原料、batch_id→批次编号。"""
    if name in _ATTR_CN:
        return _ATTR_CN[name]
    for suf in ("_id", "_code", "_key"):
        if name.endswith(suf):
            base = name[:-len(suf)]
            return (_ATTR_CN.get(base, base) if base else "编号") + "编号"
    words = [w for w in str(name).replace("-", "_").lower().split("_") if w]
    if not words:
        return name
    parts = [_ATTR_CN.get(_deplural(w)) or _ATTR_CN.get(w) or w for w in words]
    joined = "".join(parts)
    # 拼不出中文就保留原名 —— 别产出 "unknowncol" 这种半英文怪名
    return joined if any("\u4e00" <= ch <= "\u9fff" for ch in joined) else name


def llm_enhance(schema: dict, use_llm: bool = True) -> dict:
    """中文 label + 定义增强（实体 + 属性）。

    参考 sme modeling.llm_enhance：规则引擎兜底（确定性中文名 + 结构性定义）+ LLM 可选精修，
    失败/无 key/断网一律回落规则，不阻塞建模。label/definition 写入 schema，
    to_nt / 导出层用中文 RDFS label 与 skos:definition 展示（国标表1/表2 描述项）。
    """
    # 1) 规则兜底：保证所有实体/属性有中文 label（零 token，确定性）
    for e in schema.get("entities", []):
        if not e.get("label") or e["label"] == e["id"] or _looks_english(e["label"]):
            e["label"] = _entity_cn_label(e.get("table") or e["id"])
        # 定义兜底：国标表1 要求每个实体类型有 Definition。
        # 这里生成的是**结构性定义**（据实描述该实体由哪张表承载、主键、关键属性），
        # 不是语义学定义 —— 好处是零 token、离线、确定性、且每句都有实件依据；
        # 已有手写/LLM 语义定义时一律不覆盖。
        if not e.get("definition"):
            e["definition"] = _entity_cn_definition(e)
        for a in e.get("attributes", []):
            if not a.get("label") or a["label"] == a["name"] or _looks_english(a["label"]):
                a["label"] = _attr_cn_label(a["name"])
            if not a.get("definition"):
                owner = e.get("label") or e.get("id")
                a["definition"] = f"{owner}的{a['label']}。"
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


def _infer_domain(table: str, cols) -> str:
    """表 → 业务域（规则兜底，域无关）：按表名 + 列名词根匹配通用业务域。

    为什么要这一步：导出层做"根→域→实体"三层层次需要 entity.domain，
    而自动推断的 schema 原本没有该字段 → 三层层次对自动库失效。这里补上。
    词表是**通用业务域概念**（销售/采购/生产/质量/库存/设备/财务），不绑定具体行业。
    匹配不到 → "业务域"（仍有一层，不让实体裸挂根）。
    """
    txt = (str(table) + " " + " ".join(str(c) for c in cols)).lower()
    for dom, keys in _DOMAIN_KEYWORDS:
        if any(k in txt for k in keys):
            return dom
    return "业务域"


# 通用业务域词表（有序：先匹配到的域优先）。中英双语词根，覆盖常见企业台账列名。
_DOMAIN_KEYWORDS = [
    ("销售域", ("sale", "order", "customer", "订单", "客户", "销售")),
    ("采购域", ("purchase", "supplier", "raw_material", "material", "采购", "供应商", "原料")),
    ("质量域", ("qc", "quality", "inspect", "defect", "质检", "质量", "检验")),
    ("设备域", ("equipment", "device", "machine", "maintenance", "设备", "维护")),
    ("库存域", ("inventory", "stock", "warehouse", "库存", "仓储")),
    ("财务域", ("invoice", "payment", "account", "cost", "财务", "账", "成本")),
    ("生产域", ("batch", "produce", "product", "manufactur", "workshop", "生产", "批次", "产品")),
]


# ═══════════ 类层次派生 + 实体 Definition 派生（确定性 · 证据驱动 · 可复算）═══════════
# 目标（国标 GB/T 48000.3 §5.3 派生层次 + 表1 Definition 描述项）：
#   自动建模产出的本体不再是「实体平铺」——每个实体都挂到一条可解释的派生链上。
# 红线：
#   · 每条父子关系都**写出依据**（用了哪个字段/取值/命名证据），并可从同一输入复算；
#   · 全部走**纯规则**（零 token、离线、确定性），模型只能作为「建议候选」另路补入；
#   · 这里只产出**建议**，是否写入本体由人在环确认（见 api_server confirm 的硬校验）。
HIER_ROOT = "Enterprise"        # 根类（国标 §5.3 起算点）
HIER_TOP = "BusinessObject"     # 一级派生类：所有业务实体之共同上层类

# 不宜作为命名词干的通用词（避免把 id/name 这类列级词当成业务分组）
_GENERIC_STEMS = {"id", "name", "code", "key", "type", "no", "data", "info",
                  "list", "detail", "item", "value", "date", "time"}


def _table_prefix_tokens(tables) -> list:
    """返回所有表名**共有的前导 token**（库/行业前缀，如 valve_/food_）。

    只有在**全部**表名前部都出现才剥离，避免把业务词误当库前缀；
    最多剥 2 个，且剥完每张表至少还剩 1 个 token（否则不剥）。
    """
    tabs = [t for t in tables if t]
    if len(tabs) < 2:
        return []
    tok_lists = [[x for x in str(t).lower().split("_") if x] for t in tabs]
    if not all(tok_lists):
        return []
    pref = []
    for i in range(min(len(t) for t in tok_lists)):
        tok = tok_lists[0][i]
        if all(t[i] == tok for t in tok_lists):
            pref.append(tok)
        else:
            break
    pref = pref[:2]
    if any(len(t) - len(pref) < 1 for t in tok_lists):
        return []
    return pref


def _strip_table_prefix(table, pref) -> str:
    """剥掉库前缀后的表名（valve_batches --pref=[valve]--> batches）。"""
    toks = [x for x in str(table).lower().split("_") if x]
    return "_".join(toks[len(pref):]) or str(table).lower()


def _domain_evidence(table, cols):
    """表 → (业务域, 命中关键词列表)。关键词列表就是「业务域归属」这条依据的实件。

    与 _infer_domain 同口径（同一 _DOMAIN_KEYWORDS、同一匹配顺序），
    但额外把**命中的关键词**返回出来，使派生依据可写、可核。
    """
    txt = (str(table) + " " + " ".join(str(c) for c in cols)).lower()
    for dom, keys in _DOMAIN_KEYWORDS:
        hit = [k for k in keys if k in txt]
        if hit:
            return dom, hit
    return "业务域", []


def _is_number(s) -> bool:
    try:
        float(str(s).strip())
        return True
    except (TypeError, ValueError):
        return False


def derive_class_hierarchy(schema: dict, data: dict = None) -> dict:
    """派生类层次**建议**（确定性 · 证据驱动 · 可复算）。

    规则表（按优先级）：
      R0-根       Enterprise → BusinessObject
                  依据：国标 GB/T 48000.3 §5.3 规定的根类/一级派生类（结构公理）
      R1-业务域   实体 → <业务域类>（<业务域类> → BusinessObject）
                  依据：表名/列名命中 _DOMAIN_KEYWORDS 的具体关键词（写出命中词）
      R2-命名词干 同域内、去库前缀后表名共享**前导词干**的实体（≥2 个）→ <词干>Group
                  依据：共享词干 + 全部成员表名（写出证据）
    返回 {"nodes":[...], "assign":{entity_id: parent_name}, "prefix":[...]}
      nodes 每项 {name, parent, label, kind, rule, evidence}；kind ∈ root/domain/stem/entity。
    同输入同输出：全部为纯函数（只读 schema/data，遍历顺序确定）。
    """
    ents = list(schema.get("entities", []))
    tables = [e.get("table") or e.get("id") for e in ents]
    pref = _table_prefix_tokens(tables)

    nodes = [
        {"name": HIER_ROOT, "parent": None, "label": "企业", "kind": "root",
         "rule": "R0-根", "evidence": "国标 GB/T 48000.3 §5.3 规定的根类"},
        {"name": HIER_TOP, "parent": HIER_ROOT, "label": "业务对象", "kind": "root",
         "rule": "R0-根", "evidence": "国标 §5.3 一级派生类：所有业务实体之共同上层类"},
    ]

    # 每实体的业务域 + 证据（命中关键词）
    dom_of = {}
    for e in ents:
        table = e.get("table") or e.get("id")
        rows = (data or {}).get(table) or []
        cols = (list(rows[0].keys()) if rows else
                [a.get("name") for a in e.get("attributes", [])])
        dom, hit = _domain_evidence(table, cols)
        if not hit and e.get("domain"):
            # 实体已带 domain（外部声明）→ 认它，但依据如实标为「实体已声明 domain」
            dom, hit = str(e.get("domain")), []
        dom_of[e["id"]] = (dom, hit)

    # R1：业务域类节点（去重，按首次出现顺序）
    domain_order = []
    for e in ents:
        dom = dom_of[e["id"]][0]
        if dom not in domain_order:
            domain_order.append(dom)
    for dom in domain_order:
        hits = sorted({h for e in ents if dom_of[e["id"]][0] == dom for h in dom_of[e["id"]][1]})
        if hits:
            ev = "表名/列名命中业务域关键词 %s（_DOMAIN_KEYWORDS 命中项）" % (", ".join(hits))
        else:
            ev = "实体已声明业务域 '%s'（domain 字段）" % dom
        nodes.append({"name": dom, "parent": HIER_TOP, "label": dom, "kind": "domain",
                      "rule": "R1-业务域", "evidence": ev})

    # R2：同域内命名词干分组（≥2 个成员才建子类）
    stem_node_of = {}     # stem key -> node name
    assign = {}           # entity id -> parent name
    stem_members = {}     # (dom, stem) -> [entity...]
    for e in ents:
        dom = dom_of[e["id"]][0]
        table = e.get("table") or e.get("id")
        toks = [x for x in _strip_table_prefix(table, pref).split("_") if x]
        stem = _singular(toks[0]) if toks else ""
        if not stem or stem in _GENERIC_STEMS or len(stem) < 3:
            continue
        stem_members.setdefault((dom, stem), []).append(e)
    for (dom, stem) in sorted(stem_members.keys()):
        mem = stem_members[(dom, stem)]
        if len(mem) < 2:
            continue
        name = _cap(stem) + "Group"
        if name in stem_node_of.values():
            name = _cap(dom.replace("域", "")) + _cap(stem) + "Group"
        tabs = sorted((x.get("table") or x["id"]) for x in mem)
        ev = ("去库前缀%s后表名共享前导词干 '%s'（成员表: %s）"
              % (("'" + "_".join(pref) + "' ") if pref else "无", stem, "、".join(tabs)))
        nodes.append({"name": name, "parent": dom,
                      "label": (_entity_cn_label(stem) or stem) + "类实体",
                      "kind": "stem", "rule": "R2-命名词干", "evidence": ev})
        stem_node_of[(dom, stem)] = name
        for x in mem:
            assign[x["id"]] = name

    # 实体节点：未进 R2 子类的直接挂业务域类
    for e in ents:
        dom, hit = dom_of[e["id"]]
        parent = assign.get(e["id"]) or dom
        if parent in stem_node_of.values():
            ev = "归入子类 %s（依据 R2-命名词干）" % parent
        else:
            ev = ("归入业务域类 %s（依据 R1-业务域，命中关键词 %s）"
                  % (dom, ", ".join(sorted(hit)) if hit else "—（实体已声明 domain）"))
        nodes.append({"name": e["id"], "parent": parent,
                      "label": e.get("label") or e["id"], "kind": "entity",
                      "rule": "R2-命名词干" if parent in stem_node_of.values() else "R1-业务域",
                      "evidence": ev})
    return {"nodes": nodes, "assign": assign, "prefix": pref}


def apply_class_hierarchy(schema: dict, nodes: list) -> dict:
    """把（人确认后的）层次节点写回 schema：实体置 parent，非实体类进 class_hierarchy。

    幂等：重复调用结果一致。nodes 里被删掉的实体 → 保持无 parent（confirm 会拦下）。
    """
    ents = {e["id"]: e for e in schema.get("entities", [])}
    for e in ents.values():
        e.pop("parent", None)
        e.pop("parent_evidence", None)
    ch = []
    for n in nodes or []:
        name, kind = n.get("name"), n.get("kind")
        if not name:
            continue
        if kind == "entity" or name in ents:
            if name in ents and n.get("parent"):
                ents[name]["parent"] = n["parent"]
                ents[name]["parent_evidence"] = n.get("evidence", "")
        else:
            ch.append({"name": name, "parent": n.get("parent"),
                       "label": n.get("label") or name, "kind": kind,
                       "rule": n.get("rule", ""), "evidence": n.get("evidence", "")})
    if ch:
        schema["class_hierarchy"] = ch
    else:
        schema.pop("class_hierarchy", None)
    return schema


# ═══════════ 扩展描述项：具名子类派生（GB/T 48000.3 表1 HasSubclass）═══════════
# 治「实体扩展描述项齐备率 75%」：实体只有上属类(parent)、没有具名子类(HasSubclass)。
# 只从**真实分类列的真实取值**派生具名子类，逐条带依据、同输入同输出、可复算；
# 不猜同义、不伪造关系；是否写入本体由人在「扩展描述项确认」中拍板（不确认不落库）。
#   · 分类列判据：列名（小写）落在 _KIND_COLUMN_PRIORITY 白名单内（种类/状态/结论语义），
#     排除日期列（produce_date 等）、度量列、外键列 —— 它们不是「种类」。
#   · 取值域基数须在 [_SUB_MIN, _SUB_MAX] 之间：太窄(1)不成子类，太宽(>8)是标识而非种类。
#   · 每个实体至多取一个分类列：按优先级取最贴近「种类」语义者（type > category > … > result）。
_SUB_MIN = 2
_SUB_MAX = 8
_KIND_COLUMN_PRIORITY = (
    "type", "device_type", "product_type", "category", "kind", "class",
    "part_name", "check_item", "material", "storage", "connection",
    "credit_level", "status", "state", "result",
)


def _sub_local_name(value: str) -> str:
    """取值 → 局部名片段（仅保留中文/字母/数字，其余转下划线；确定性）。"""
    out = []
    for ch in str(value).strip():
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    s = "".join(out).strip("_")
    return s or "value"


def _kind_column_of(ent: dict, data: dict) -> tuple:
    """挑选实体最能代表「种类」的分类列 + 其真实取值分布。

    返回 (col, [(value, count), ...])；无合格分类列返回 (None, [])。
    确定性：按 _KIND_COLUMN_PRIORITY 优先级取第一个合格列；取值按 (计数降序, 值升序) 排序。
    """
    table = ent.get("table")
    rows = (data or {}).get(table) if table else None
    if not rows:
        return None, []
    cols = list(rows[0].keys())
    present = [c for c in _KIND_COLUMN_PRIORITY if c in cols]
    for col in present:
        vals = [str(r.get(col)).strip() for r in rows
                if r.get(col) is not None and str(r.get(col)).strip() != ""]
        distinct = sorted(set(vals))
        if _SUB_MIN <= len(distinct) <= _SUB_MAX:
            from collections import Counter as _C
            cnt = _C(vals)
            ordered = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
            return col, ordered
    return None, []


def derive_named_subclasses(schema: dict, data: dict = None) -> list:
    """派生「具名子类」**建议**（确定性 · 证据驱动 · 可复算）。

    每条建议 {name, label, parent, kind, column, value, count, rule, evidence}：
      · name    子类 id（= 父实体 id + '__' + 取值），全局唯一
      · parent  父实体 id（本体的 subClassOf 目标）
      · rule    R3-具名子类
      · evidence 写明：依据哪张表、哪个字段、哪个真实取值、命中多少行
    同输入同输出（纯函数，遍历/排序确定）。
    """
    out = []
    for e in schema.get("entities", []):
        if e.get("kind") == "subclass":      # 已是子类的不再往下派生（不递归）
            continue
        table = e.get("table")
        col, ordered = _kind_column_of(e, data)
        if not col:
            continue
        total = sum(c for _v, c in ordered)
        for v, cnt in ordered:
            out.append({
                "name": "%s__%s" % (e["id"], _sub_local_name(v)),
                "label": str(v),
                "parent": e["id"],
                "kind": "subclass",
                "column": col,
                "value": str(v),
                "count": cnt,
                "rule": "R3-具名子类",
                "evidence": ("依据表 %s 的分类列 '%s' 的真实取值 '%s'（命中 %d/%d 行）；"
                             "该列取值域 %d 个，属可枚举种类"
                             % (table, col, v, cnt, total, len(ordered))),
                "definition": ("%s中 %s 为『%s』的具体种类（依据表 %s 字段 %s 的真实取值，%d 行）。"
                               % (e.get("label") or e["id"], col, v, table, col, cnt)),
            })
    return out


def apply_named_subclasses(schema: dict, subs: list) -> dict:
    """把（人确认后的）具名子类写回 schema：作为**类实体**（table=None，不产实例）。

    幂等：先清掉本 schema 里此前落库的全部 subclass 类实体，再按 subs 重建；
    属性从父实体继承（IS-A：子类共享父类属性），继承来的属性不重复占实例。
    """
    ents = schema.get("entities", [])
    by_id = {e["id"]: e for e in ents}
    # 清掉旧的子类实体（保持幂等，避免重复追加）
    schema["entities"] = [e for e in ents if e.get("kind") != "subclass"]
    ents = schema["entities"]
    by_id = {e["id"]: e for e in ents}
    for s in subs or []:
        if not s.get("name") or not s.get("parent"):
            continue
        parent = by_id.get(s["parent"])
        if not parent:
            continue
        sub = {
            "id": s["name"],
            "label": s.get("label") or s["name"],
            "table": None,
            "key": None,
            "domain": parent.get("domain"),
            "kind": "subclass",
            "definition": s.get("definition") or "",
            "definition_evidence": s.get("evidence") or "",
            "parent": s["parent"],
            "parent_evidence": s.get("evidence") or "",
            "parent_rule": s.get("rule") or "R3-具名子类",
            "subclass_column": s.get("column"),
            "subclass_value": s.get("value"),
            "attributes": json.loads(json.dumps(parent.get("attributes") or [])),
        }
        ents.append(sub)
        by_id[sub["id"]] = sub
    schema["subclasses"] = [dict(s) for s in (subs or [])]
    return schema


def derive_definitions(schema: dict, data: dict = None) -> list:
    """派生实体 Definition **建议**（结构性定义 + 取值样例；确定性、可复算、有依据）。

    定义构成：业务域/承载表/唯一标识/关键字段（来自 schema 实件）
              + 取值样例（来自该表真实行，最多 2 个文本列的各自前 2 个取值）。
    每条带 evidence（用了哪些字段与取值）；纯规则、零 token（模型仅可作为候选另路补入）。
    """
    out = []
    for e in schema.get("entities", []):
        table = e.get("table") or e.get("id")
        rows = (data or {}).get(table) or []
        base = (e.get("definition") or "").strip() or _entity_cn_definition(e)
        samples = []
        fields_used = []
        for a in e.get("attributes", []):
            aname = a.get("name")
            if not aname or aname == e.get("key"):
                continue
            vals = [str(r.get(aname)).strip() for r in rows if r.get(aname) not in (None, "")]
            vals = [v for v in vals if v and not _is_number(v)]
            if vals:
                samples.append("%s=%s" % (a.get("label") or aname, "/".join(vals[:2])))
                fields_used.append(aname)
            if len(samples) >= 2:
                break
        text = base
        if samples:
            text = text.rstrip("。") + "；取值样例（" + "；".join(samples) + "）。"
        cols = [a.get("name") for a in e.get("attributes", [])]
        ev = "依据表 %s 的字段 %s" % (table, cols)
        if samples:
            ev += "；取值样例来自字段 %s（真实行）" % fields_used
        out.append({"entity": e["id"], "definition": text,
                    "rule": "R-DEF 结构(域/表/主键/关键字段)+取值样例",
                    "evidence": ev, "source": "rule"})
    return out


def suggest_schema(data: dict, use_llm: bool = True, industry: str = None) -> dict:
    """从多表数据自动推断 schema（schema-free，无需手写 ontology_schema.json）。

    遍历 {表名: [行...]}，每表建一个实体（id=表名首字母大写，key=id 或 *_id 或首列，
    attributes=行字段名 + 自动推断类型，复用 _infer_prop_role 标注语义角色）；
    复用 _infer_relations 推断跨表关系；每表主键生成 unique 约束。
    industry: 传入后写入 schema，触发按行业的命名空间隔离
              （resolve_namespace 走 {org}industry/{industry}#，国标第9章要求）。
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
        entities.append({"id": eid, "label": eid, "table": table, "key": key,
                         "domain": _infer_domain(table, sample.keys()),
                         "attributes": attributes})
        constraints.append({"type": "unique", "on": f"{eid}.{key}", "msg": f"{eid} 主键 {key} 唯一"})
    schema = {
        "version": "1.0",
        "name": "auto-inferred-ontology",
        "entities": entities,
        "relations": (_rels := _infer_relations(data))
        + (_infer_relations_llm(data, _rels) if use_llm else []),
        "constraints": constraints,
    }
    # 行业命名空间隔离（国标第9章）：显式传入才生效，未传保持旧默认字节不变
    if industry:
        schema["industry"] = industry
    # 与 load_schema 对齐：注入 {id: entity} 索引，供 build_graph/validate/to_nt 直接消费
    schema["_entities"] = {e["id"]: e for e in entities}
    # 中文 label 增强（规则兜底 + LLM 可选精修），label 供 to_nt RDFS label 中文展示。
    # use_llm=False 时纯规则兜底: 确定性、毫秒级 —— 质量门/批量体检这类只需结构的场景走这条。
    schema = llm_enhance(schema, use_llm=use_llm)
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
# ── 命名空间（GB/T 48000.3 §9 扩展原则：支持版本化 + 行业子路径；缺省保持原值，向后兼容） ──
DEFAULT_NS = "http://factory.example/ontology#"
NS = DEFAULT_NS                      # 模块级默认：旧调用方/未声明 namespace 的 schema 行为不变
NS_ORG = "https://ontology.example.com/"   # 组织根命名空间（新方案：{根}/{域}# + 行业子路径）
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATAPROP = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
_NS_URI = "http://www.w3.org/2001/XMLSchema#"
# 标准词汇（v0.x.1 新增：本体头 / 定义 / 公理）
OWL_ONTOLOGY = "<http://www.w3.org/2002/07/owl#Ontology>"
OWL_VERSIONIRI = "<http://www.w3.org/2002/07/owl#versionIRI>"
OWL_RESTRICTION = "<http://www.w3.org/2002/07/owl#Restriction>"
OWL_ONPROPERTY = "<http://www.w3.org/2002/07/owl#onProperty>"
OWL_MAXCARD = "<http://www.w3.org/2002/07/owl#maxCardinality>"
OWL_MINQUALIFIED = "<http://www.w3.org/2002/07/owl#minQualifiedCardinality>"
OWL_ONCLASS = "<http://www.w3.org/2002/07/owl#onClass>"
SKOS_DEFINITION = "<http://www.w3.org/2004/02/skos/core#definition>"

# 属性类型 → xsd 映射（国标表2 range 项；未列出的回退 xsd:string）
_XSD_BY_TYPE = {"number": "decimal", "integer": "integer", "date": "date",
                "boolean": "boolean", "string": "string"}


def resolve_namespace(schema: dict) -> dict:
    """解析 schema 的命名空间治理字段（缺省全部可推导，不阻塞）。

    支持字段（全可选）：
      namespace / prefix / name / label / version / version_iri / org / industry
    返回 {ns, prefix, version_iri, name, label}。
    """
    org = str(schema.get("org") or NS_ORG)
    if not org.endswith(("/", "#")):
        org += "/"
    industry = str(schema.get("industry") or "").strip("/")
    name = str(schema.get("name") or "ontology").strip("/") or "ontology"
    # 命名空间解析(向后兼容关键): 显式 namespace > 显式 org/industry(新方案) > 原硬编码默认值
    explicit = str(schema.get("namespace") or "")
    if explicit:
        ns = explicit
    elif industry:
        ns = f"{org}industry/{industry}#"          # 行业扩展命名空间(国标第9章: 新命名空间+不冲突)
    elif schema.get("org"):
        ns = f"{org}{name}#"
    else:
        ns = DEFAULT_NS                            # 未声明 → 保持原值, 旧产物字节不变
    prefix = str(schema.get("prefix") or "o")
    ver = str(schema.get("version") or "").strip()
    viri = str(schema.get("version_iri") or "")
    if not viri:
        base = ns[:-1] if ns.endswith(("#", "/")) else ns
        viri = f"{base}/{ver}" if ver else base
    return {"ns": ns, "prefix": prefix, "version_iri": viri, "name": name,
            "label": str(schema.get("label") or name)}


def _pascal(name: str) -> str:
    """id/列名 → PascalCase 局部名（IRI 自动补全用）。"""
    parts = [p for p in str(name).replace("-", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) if parts else "Item"


def fill_iris(schema: dict) -> dict:
    """按命名空间自动补全实体/属性的 iri（国标表1/表2 的 IRI 描述项）。

    已有 iri 尊重原值；缺省用 namespace + PascalCase(id) / camelCase(attr)，写回 schema（幂等）。
    """
    m = resolve_namespace(schema)
    ns = m["ns"]
    for e in schema.get("entities", []):
        if not e.get("iri"):
            # 与 to_nt 的 eid_to_cls 保持一致(表名 capitalize)，避免类 URI 与实例引用不一致
            local = str(e["table"]).capitalize() if e.get("table") else _pascal(e.get("id", ""))
            e["iri"] = ns + local
        for a in e.get("attributes", []):
            if not a.get("iri"):
                a["iri"] = ns + _local_name(a.get("name", ""))
    for r in schema.get("relations", []):
        if not r.get("iri"):
            r["iri"] = ns + _local_name(r.get("id", "rel"))
    schema["_ns"] = m
    return schema


def build_class_hierarchy(schema: dict) -> list:
    """类型体系：显式 parent 优先（国标 §5.3 根→一级→二级派生），回退 domain 分组 + 根 Enterprise。

    · schema 带 class_hierarchy（自助建模「层次与定义确认」落库的类节点）→ 以它为准，
      再把各实体按 parent 挂上去（派生链可含根/业务域/命名词干子类）。
    · 否则 schema 声明 entity.parent 时按声明建层次；未声明则回退 domain 分组（向后兼容）。
    """
    ents = schema.get("entities", [])
    ch = schema.get("class_hierarchy") or []
    if ch:
        nodes = []
        seen = set()
        for n in ch:
            nm = n.get("name")
            if not nm or nm in seen:
                continue
            nodes.append({"name": nm, "super": n.get("parent"),
                          "label": n.get("label") or nm})
            seen.add(nm)
        for e in ents:
            nodes.append({"name": e["id"], "super": e.get("parent") or "BusinessObject",
                          "label": e.get("label") or e["id"]})
        return nodes
    if any(e.get("parent") for e in ents):
        nodes = [{"name": "Enterprise", "super": None, "label": "企业"},
                 {"name": "BusinessObject", "super": "Enterprise", "label": "业务对象"}]
        seen = {"Enterprise", "BusinessObject"}
        for e in ents:
            p = e.get("parent") or "BusinessObject"
            if p not in seen:
                nodes.append({"name": p, "super": "BusinessObject", "label": p})
                seen.add(p)
            nodes.append({"name": e["id"], "super": p, "label": e.get("label") or e["id"]})
        return nodes
    return _hierarchy_by_domain(schema)


def _hierarchy_by_domain(schema: dict) -> list:
    """原 domain 分组层次（无显式 parent 时的回退）。"""
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

def _nt_class_decls(entities, eid_to_cls, L, ns, m, class_hierarchy=None):
    """类声明：本体头 + 根类 + 每实体 owl:Class（label / skos:definition / subClassOf）。

    覆盖 GB/T 48000.3 §5.3-5.5：类声明、中文 label、中文定义、类层次派生。
    class_hierarchy（自助建模确认落库的类节点）先声明，保证实体 subClassOf 的目标类
    都是有定义的主体（不产生悬空引用）。
    """
    L.append(f"<{ns}> {RDF_TYPE} {OWL_ONTOLOGY} .")
    L.append(f"<{ns}> {OWL_VERSIONIRI} <{m['version_iri']}> .")
    L.append(f"<{ns}> {RDFS_LABEL} {_q(m['label'])} .")
    declared = set(eid_to_cls.values())
    # 类层次节点（根/业务域/命名词干子类）→ owl:Class + subClassOf
    for n in (class_hierarchy or []):
        nm = n.get("name")
        if not nm:
            continue
        cls = _pascal(nm)
        if cls in declared:
            continue
        L.append(f"<{ns}{cls}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{ns}{cls}> {RDFS_LABEL} {_q(n.get('label') or nm)} .")
        if n.get("parent"):
            L.append(f"<{ns}{cls}> {RDFS_SUBCLASS} <{ns}{_pascal(n['parent'])}> .")
        declared.add(cls)
    # 显式 parent 但非已声明实体的父类（如 BusinessObject / 领域类）→ 单独声明为 owl:Class
    for ent in entities.values():
        p = ent.get("parent")
        if p and p not in entities:
            pcls = _pascal(p)
            if pcls not in declared:
                L.append(f"<{ns}{pcls}> {RDF_TYPE} {OWL_CLASS} .")
                L.append(f"<{ns}{pcls}> {RDFS_LABEL} {_q(p)} .")
                declared.add(pcls)
    for eid, ent in entities.items():
        cls_uri = ent.get("iri") or (ns + eid_to_cls[eid])
        L.append(f"<{cls_uri}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{cls_uri}> {RDFS_LABEL} {_q(ent.get('label') or eid_to_cls[eid])} .")
        if ent.get("definition"):
            L.append(f"<{cls_uri}> {SKOS_DEFINITION} {_q(ent['definition'])} .")
        if ent.get("equivalent_class"):
            L.append(f"<{cls_uri}> <http://www.w3.org/2002/07/owl#equivalentClass> {ent['equivalent_class']} .")
        p = ent.get("parent")
        if p:
            pcls = eid_to_cls.get(p) or _pascal(p)
            L.append(f"<{cls_uri}> {RDFS_SUBCLASS} <{ns}{pcls}> .")


def _nt_property_decls(entities, relations, data, eid_to_cls, L, ns):
    """数据属性 + 对象属性声明（跳过主键/外键列，对象属性用关系英文id）。

    覆盖 GB/T 48000.3 表2 描述项：IRI/Name/Label/Domain/Range/typeofTerms(+skos:definition)。
    """
    for eid, ent in entities.items():
        cls_uri = ent.get("iri") or (ns + eid_to_cls[eid])
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in relations if (r.get("fk") or "").startswith(table + ".")}
        for attr in ent.get("attributes", []):
            aname = attr["name"]
            if aname == key or aname in rel_cols:
                continue
            p = _local_name(aname)
            vals = [r.get(aname) for r in data[table] if r.get(aname)]
            t = _guess_type(vals[0]) if vals else "xsd:string"
            L.append(f"<{ns}{p}> {RDF_TYPE} {OWL_DATAPROP} .")
            L.append(f"<{ns}{p}> {RDFS_DOMAIN} <{cls_uri}> .")
            # range: 显式 range 优先，否则按值类型映射
            rng = attr.get("range") or (_NS_URI + _XSD_BY_TYPE.get(attr.get("type"), t.split(":")[1]))
            if str(rng).startswith("http"):
                L.append(f"<{ns}{p}> {RDFS_RANGE} <{rng}> .")
            else:
                L.append(f"<{ns}{p}> {RDFS_RANGE} <{_NS_URI}{str(rng).split(':')[-1]}> .")
            L.append(f"<{ns}{p}> {RDFS_LABEL} {_q(attr.get('label') or aname)} .")
            if attr.get("definition"):
                L.append(f"<{ns}{p}> {SKOS_DEFINITION} {_q(attr['definition'])} .")
        # 纯连接表被折叠为直连后(_nt_instances 里已把非主键 FK 提升为 父→目标 边):
        #   · 非主键 FK 关系的 domain 上提到父类(否则 domain 指向一个没有实例的空类, 语义冲突)
        #   · 主键 FK 关系(父连接)已被吸收, 不再声明
        own_attrs = [a["name"] for a in ent.get("attributes", [])
                     if a["name"] != key and a["name"] not in rel_cols]
        folded_uri = None
        if ent.get("detail") and not own_attrs:
            _pr = next((r for r in relations
                        if (r.get("fk") or "").startswith(table + ".") and r["fk"].split(".")[1] == key), None)
            if _pr:
                folded_uri = f"{ns}{eid_to_cls.get(_pr['to'], _pr['to'])}"
        for r in relations:
            if (r.get("fk") or "").startswith(table + "."):
                rel = r.get("iri") or (ns + r["id"])   # 对象属性 URI(显式 iri 优先, 否则关系 id)
                if folded_uri:
                    if r["fk"].split(".")[1] == key:
                        continue                        # 父连接关系已吸收
                    dom_uri = folded_uri                # domain 上提到父类
                else:
                    dom_uri = cls_uri
                L.append(f"<{rel}> {RDF_TYPE} {OWL_OBJPROP} .")
                L.append(f"<{rel}> {RDFS_DOMAIN} <{dom_uri}> .")
                L.append(f"<{rel}> {RDFS_RANGE} <{ns}{eid_to_cls.get(r['to'], r['to'])}> .")
                L.append(f"<{rel}> {RDFS_LABEL} {_q(r.get('label', '关联'))} .")
                if r.get("definition"):
                    L.append(f"<{rel}> {SKOS_DEFINITION} {_q(r['definition'])} .")


def _nt_category_hierarchy(entities, data, eid_to_cls, L, ns):
    """类别类层级 + 类型体系(subClassOf)：实体含 category 列 → Category 类 + isA。"""
    for eid, ent in entities.items():
        cls = eid_to_cls[eid]
        cls_uri = ent.get("iri") or (ns + cls)
        table = ent.get("table")
        if table not in data or not data[table] or "category" not in data[table][0]:
            continue
        cat_cls = f"{cls}Category"
        L.append(f"<{ns}{cat_cls}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{ns}{cat_cls}> {RDFS_SUBCLASS} <{cls_uri}> .")
        for row in data[table]:
            cat = row.get("category")
            if cat:
                cat_uri = f"{ns}{cat_cls}_{cat}"
                L.append(f"<{cat_uri}> {RDF_TYPE} {OWL_CLASS} .")
                L.append(f"<{cat_uri}> {RDFS_SUBCLASS} <{ns}{cat_cls}> .")


def _nt_instances(entities, relations, data, eid_to_cls, L, ns):
    """实例 + 数据/对象属性（FK join，明细实体按行建实例）。"""
    for eid, ent in entities.items():
        cls_uri = ent.get("iri") or (ns + eid_to_cls[eid])
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        detail = ent.get("detail", False)
        rels_of_table = [r for r in relations if (r.get("fk") or "").startswith(table + ".")]
        rel_cols = {r["fk"].split(".")[1] for r in rels_of_table}
        # 纯连接表(多对多中间表): 只有主键 + 外键, 没有自身列。
        # 通用判据, 域无关 —— 这类表若建成 N-ary 实体, 检索要多跳一层(父→连接实体→目标)
        # 且引入无信息节点, 反而降低精度; 直接把非主键外键提升为 父→目标 直连边。
        # 一旦该表出现自身列(用量/时间/操作人…), own_attrs 非空, 自动回到 N-ary 建实体。
        own_attrs = [a["name"] for a in ent.get("attributes", [])
                     if a["name"] != key and a["name"] not in rel_cols]
        if detail and not own_attrs:
            parent_rel = next((r for r in rels_of_table if r["fk"].split(".")[1] == key), None)
            if parent_rel:
                pcls = eid_to_cls.get(parent_rel["to"], parent_rel["to"])
                for row in data[table]:
                    pv = row.get(key)
                    if pv is None or str(pv).strip() == "":
                        continue
                    pinst = f"{ns}{pcls}_{pv}"
                    for r in rels_of_table:
                        fcol = r["fk"].split(".")[1]
                        if fcol == key or not row.get(fcol):
                            continue
                        rel = r.get("iri") or (ns + r["id"])
                        tcls = eid_to_cls.get(r["to"], r["to"])
                        L.append(f"<{pinst}> <{rel}> <{ns}{tcls}_{row[fcol]}> .")
            continue
        seen_ids = set()
        for i, row in enumerate(data[table]):
            kid = row.get(key) or f"{i+1}"
            if str(kid) in seen_ids:
                kid = f"{kid}_{i}"
            seen_ids.add(str(kid))
            inst_uri = f"{cls_uri}_{kid}" + (f"@{i}" if detail else "")
            L.append(f"<{inst_uri}> {RDF_TYPE} <{cls_uri}> .")
            if "category" in row and row.get("category"):
                L.append(f"<{inst_uri}> <{ns}hasCategory> <{ns}{eid_to_cls[eid]}Category_{row['category']}> .")
            for attr in ent.get("attributes", []):
                aname = attr["name"]
                if aname == key or aname in rel_cols:
                    continue
                if aname in row and row.get(aname) is not None and str(row.get(aname)).strip() != "":
                    p = _local_name(aname)
                    t = _guess_type(row[aname])
                    L.append(f"<{inst_uri}> <{ns}{p}> {_q(row[aname])}^^<{_NS_URI}{t.split(':')[1]}> .")
            for r in rels_of_table:
                fcol = r["fk"].split(".")[1]
                if fcol in row and row.get(fcol):
                    rel = r.get("iri") or (ns + r["id"])
                    L.append(f"<{inst_uri}> <{rel}> <{ns}{eid_to_cls.get(r['to'], r['to'])}_{row[fcol]}> .")


def to_nt(data: dict, schema: dict, outpath: str = None) -> list:
    """把 schema 驱动建模结果输出成标准 N-Triples（统一替代 csv_to_owl/multi_table）。

    从 schema(实体/关系/约束) + data(多表) 生成：类声明、数据属性、对象属性、
    实例、类别类层级、类型体系。格式与 csv_to_owl/multi_table 对齐，下游
    ontology_qa_v3.parse_nt / graph_rag.parse_nt 无缝消费。

    outpath 非空则写文件，返回行列表。
    """
    L = []
    entities = schema.get("_entities", schema)
    # 命名空间 + IRI 自动补全（幂等；schema 已填 _ns 则复用）
    schema = fill_iris(schema)
    m = schema.get("_ns") or resolve_namespace(schema)
    ns = m["ns"]
    declared_fks = {r.get("fk") for r in schema.get("relations", []) if r.get("fk")}
    inferred = [r for r in _infer_relations(data) if r.get("fk") not in declared_fks]
    relations = list(schema.get("relations", [])) + inferred
    # 实体 ID -> 类局部名（用表名风格，与 multi_table 下游兼容）
    eid_to_cls = {eid: (ent.get("table", eid).capitalize() if ent.get("table") else eid)
                  for eid, ent in entities.items()}
    _nt_class_decls(entities, eid_to_cls, L, ns, m, class_hierarchy=schema.get("class_hierarchy"))
    _nt_property_decls(entities, relations, data, eid_to_cls, L, ns)
    _nt_category_hierarchy(entities, data, eid_to_cls, L, ns)
    _nt_instances(entities, relations, data, eid_to_cls, L, ns)
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
