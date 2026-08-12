#!/usr/bin/env python3
"""lexicon_agent.py — 原子智能体：全自动生成问答词典。

核心：给一个数据源，自动生成完整词典（状态/类型/区域/属性映射 + 字段别名），
**无需手写任何词表**。换工厂只需提供数据，词典全自动产出。

能力：
1. 字段名→中文语义推断（flow_rate_m3h → 流量, pressure_bar → 压力）
2. 枚举值→中文名（servo_motor → 伺服电机）
3. 字段别名推断（pumpStatus → status, pumpType → deviceType）

task 结构:
  {"source_csv": "...", "out_lexicon": "...", "use_llm": true, "table_name": "..."}
"""

import os
import sys
import csv
import json
import re
import importlib.util

# 路径修正
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
sys.path.insert(0, os.path.join(_APP, "..", "src"))

from core.base_agent import BaseAgent, AgentResult

# 统一模型调用（从 model_config.json 读取，可切 ornith/deepseek）
from model_llm import llm_generate

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# 常见字段名模式 -> 标准字段 (用于别名推断)
STD_FIELD_PATTERNS = {
    "status": ["status", "state", "condition", "runstate", "运行状态", "pump_status"],
    "deviceType": ["type", "category", "class", "kind", "device_type", "pump_type"],
    "deviceName": ["name", "title", "label", "device_name", "pump_name", "item_name"],
    "location": ["zone", "area", "region", "district", "location", "region", "区域"],
}


# ── 字段类别启发式关键词（供 _build_enum_mapping 数据驱动查表）──
STATUS_KEYWORDS = ("status", "state", "condition", "runstate", "failure", "fault", "alarm", "flag")
TYPE_KEYWORDS = ("type", "category", "kind")
ZONE_KEYWORDS = ("zone", "district", "area", "location", "region")

class LexiconAgent(BaseAgent):
    name = "lexicon"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        src = task.get("source_csv")
        if not src or not os.path.exists(src):
            return self._err(f"数据文件不存在: {src}")
        rows, headers = self._load_csv(src)
        if not rows:
            return self._err("CSV 为空或无数据行")

        # 1. 提取字段统计（每字段: 取值集合 + 是否数值型）
        field_info = self._analyze_fields(rows, headers)

        # 2. 生成字段语义映射（LLM 优先，规则回退）
        attr_map, err = self._llm_field_mapping(field_info, task) if task.get("use_llm", True) else (None, "no_llm")
        if err or not attr_map:
            attr_map = self._rule_field_mapping(field_info)

        # 3. 生成枚举值映射（状态/类型字段的 值→中文）
        enum_map = self._llm_enum_mapping(field_info) if task.get("use_llm", True) else self._rule_enum_mapping(field_info)

        # 4. 组装完整词典
        lexicon = self._build_full_lexicon(field_info, attr_map, enum_map, task)
        out = task.get("out_lexicon") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config", "lexicon.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(lexicon, f, ensure_ascii=False, indent=2)
        return self._ok({"out_lexicon": out, "fields": len(headers),
                         "attr_map": len(lexicon.get("attr_cn2en", {})),
                         "used_llm": not bool(err)}, "lexicon")

    # ---------------- 数据加载与分析 ----------------

    def _load_csv(self, src):
        """统一读取源数据(CSV/JSON/SQLite/Excel, 复用 data_loader)"""
        from data_loader import load_table
        _, headers, rows = load_table(src)
        return rows, headers

    def _analyze_fields(self, rows, headers):
        """分析每字段: 取值集合 + 是否数值。"""
        info = {}
        for h in headers:
            vals = [r[h].strip() for r in rows if r.get(h) and r[h].strip()]
            if not vals:
                continue
            distinct = sorted(set(vals))
            is_num = all(self._is_num(v) for v in vals[:50])
            info[h] = {
                "values": distinct[:30], "num_values": len(distinct),
                "is_numeric": is_num, "sample": vals[0],
            }
        return info

    @staticmethod
    def _is_num(v):
        try:
            float(v)
            return True
        except (ValueError, TypeError):
            return False

    # ---------------- 字段名→中文语义（LLM） ----------------

    def _llm_field_mapping(self, field_info, task):
        """让 LLM 为每个字段生成中文语义名 + 判断是否状态/类型/区域字段。"""
        import requests
        desc = "\n".join(
            f"- {f}: 样本[{info['sample']}], 值数{info['num_values']}, {'数值' if info['is_numeric'] else '枚举/文本'}"
            for f, info in field_info.items())
        table = task.get("table_name", "数据")
        prompt = (
            f"你是工业数据建模师。下面是工厂'{table}'数据的字段清单。\n"
            "请为每个字段给出：中文名称(用户自然语言查询用)、是否主键、是否状态字段、是否类型字段、是否名称字段、是否位置字段。\n"
            "严格返回 JSON，不要额外文字。\n\n"
            f"【字段】\n{desc}\n\n"
            '【要求】返回 {"fields": {"字段名": {"cn": "中文名", "is_key": bool, "is_status": bool, "is_type": bool, "is_name": bool, "is_location": bool}}}'
        )
        text = llm_generate(prompt, temperature=0.1, max_tokens=2000)
        if not text or text.startswith("[模型"):
            return None, (text or "LLM 未返回 JSON")
        text = re.sub(r'```\w*\s*|\s*```', '', text).strip()
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return None, "LLM 未返回 JSON"
        data = json.loads(text[s:e+1])
        return data.get("fields", {}), None

    def _rule_field_mapping(self, field_info):
        """规则回退：字段名拆词+单位推断中文名。"""
        attr = {}
        for f, info in field_info.items():
            if info["is_numeric"]:
                cn = self._infer_cn_from_name(f)
                attr[f] = cn
        return attr

    # 常见英文词 → 中文（工业领域扩充）
    EN_CN = {
        # 基本
        "id": "编号", "code": "编码", "name": "名称", "label": "名称", "title": "标题",
        "status": "状态", "state": "状态", "condition": "状态", "type": "类型", "category": "类别",
        "class": "类别", "kind": "种类",
        # 物理量
        "power": "功率", "voltage": "电压", "current": "电流", "resistance": "电阻",
        "frequency": "频率", "speed": "转速", "rotational": "转速", "rotation": "转速",
        "torque": "扭矩", "pressure": "压力", "temperature": "温度", "temp": "温度",
        "flow": "流量", "rate": "速率", "level": "液位", "humidity": "湿度", "vibration": "振动",
        "weight": "重量", "mass": "质量", "capacity": "容量", "volume": "体积",
        "length": "长度", "width": "宽度", "height": "高度", "distance": "距离",
        # 统计/业务
        "quantity": "数量", "amount": "数量", "count": "数量", "total": "总计",
        "yield": "良品率", "efficiency": "效率", "energy": "能耗", "consumption": "能耗",
        "usage": "用量", "hours": "运行小时", "hour": "小时", "runtime": "运行时长",
        "uptime": "运行时长", "downtime": "停机时长", "age": "机龄",
        # 时间
        "date": "日期", "time": "时间", "year": "年份", "month": "月份", "day": "日期",
        "start": "开始", "end": "结束", "install": "安装", "production": "生产",
        "manufacture": "制造", "maintenance": "维护", "last": "最近",
        # 设备/位置
        "device": "设备", "machine": "机器", "equipment": "设备", "unit": "单元",
        "line": "产线", "zone": "区域", "area": "区域", "district": "区域", "location": "位置",
        "position": "位置", "region": "区域", "section": "工段",
        # 状态/属性
        "run": "运行", "running": "运行", "alarm": "报警", "fault": "故障", "failure": "故障",
        "error": "错误", "normal": "正常", "idle": "空闲", "offline": "离线",
        "sensor": "传感器", "signal": "信号", "value": "数值",
        # AI4I 预测性维护(拆词兜底)
        "air": "空气", "process": "工艺", "tool": "刀具", "wear": "磨损",
        "rotational": "转速", "torque": "扭矩", "twf": "刀具磨损故障",
        "hdf": "热耗散故障", "pwf": "功率故障", "osf": "过冲故障", "rnf": "随机故障",
        "udi": "唯一标识", "ud": "唯一标识",
    }

    def _split_parts(self, field):
        """字段名拆词：支持下划线/连字符/点/驼峰。"""
        # 先处理驼峰: rotationalSpeed -> rotational_Speed
        s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', field)
        return [p for p in re.split(r'[_\-.\s]+', s) if p]

    def _infer_cn_from_name(self, field):
        """从字段名拆词推断中文名，多词合成 + 单位后缀。"""
        parts = self._split_parts(field.lower())
        # 主词映射：跳过单位/介词/无意义词
        skip = {"kw", "mw", "w", "v", "a", "hz", "m3", "h", "m", "s", "mm", "cm", "kg",
                "of", "the", "no", "at", "in", "per", "max", "min", "avg", "mean"}
        main = []
        seen = set()
        for p in parts:
            if p in skip or len(p) == 1:
                continue
            if p in self.EN_CN:
                cn_w = self.EN_CN[p]
            else:
                # 未命中：作为原始词保留（避免丢信息）
                cn_w = p
            if cn_w not in seen:  # 去重同义词合成(rotational+speed→转速, 防"转速转速")
                seen.add(cn_w)
                main.append(cn_w)
        if not main:
            return field
        return "".join(main)

    def _rule_enum_mapping(self, field_info):
        """规则回退：枚举值若已是英文，用值本身；否则原样。"""
        mapping = {}
        for f, info in field_info.items():
            if not info["is_numeric"] and 2 <= info["num_values"] <= 50:
                mapping[f] = {}
                for v in info["values"]:
                    mapping[f][v] = v
        return mapping

    # ---------------- 枚举值→中文名（LLM） ----------------

    def _llm_enum_mapping(self, field_info):
        """让 LLM 为状态/类型枚举值生成中文名。返回 {字段: {值: 中文名}}。"""
        import requests
        enum_fields = {f: info for f, info in field_info.items() if not info["is_numeric"] and 2 <= info["num_values"] <= 50}
        if not enum_fields:
            return {}
        desc = "\n".join(f"- {f}: {info['values']}" for f, info in enum_fields.items())
        prompt = (
            "你是工业数据标注员。下面字段的英文枚举值，请为每个值给中文名。\n"
            "严格返回 JSON。\n\n" f"【字段取值】\n{desc}\n\n"
            '【要求】{"mappings": {"字段名": {"英文值": "中文名", ...}}}'
        )
        text = llm_generate(prompt, temperature=0.1, max_tokens=2000)
        if not text or text.startswith("[模型"):
            return {}
        text = re.sub(r'```\w*\s*|\s*```', '', text).strip()
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return {}
        return json.loads(text[s:e+1]).get("mappings", {})

    # ---------------- 组装完整词典 ----------------

    # ── 字段类别启发式（数据驱动，降低 _build_full_lexicon 复杂度）──
    def _is_single_letter_grade(self, vals):
        """单字母等级(L/M/H/A/B)不是类型枚举，跳过。"""
        return all(isinstance(v, str) and len(v) == 1 and v.isalpha() and v.upper() in "LMHAB"
                   for v in vals[:30]) if vals else False

    def _is_binary_flag(self, vals):
        """纯布尔/0-1标记。"""
        return all(isinstance(v, str) and v.strip() in ("0", "1", "true", "false", "True", "False", "是", "否")
                   for v in vals[:30]) if vals else False

    def _camelize(self, f):
        """snake_case → CamelCase。"""
        return "".join(part.capitalize() for part in f.split("_")[0:1]) + "".join(
            part.capitalize() for part in f.split("_")[1:])

    def _build_attr_mapping(self, field_info, attr_map):
        """属性映射: attr_cn2en + attr_en2cn（数值字段）。"""
        attr_cn2en, attr_en2cn = {}, {}
        for f, info in field_info.items():
            if not info["is_numeric"]:
                continue
            cn = None
            if attr_map and isinstance(attr_map.get(f), dict):
                cn = attr_map[f].get("cn")
            elif attr_map and isinstance(attr_map.get(f), str):
                cn = attr_map[f]
            if not cn:
                cn = self._infer_cn_from_name(f)  # 修正: 原 (f,{}) 传2参但函数只收1参(潜在bug)
            attr_cn2en[cn] = f
            attr_en2cn[f] = cn
        return attr_cn2en, attr_en2cn

    def _build_enum_mapping(self, field_info, enum_map):
        """状态/类型/区域值映射（核心启发式）。"""
        status_cn2en, type_cn2en, zone_cn2en = {}, {}, {}
        for f, info in field_info.items():
            low = f.lower()
            vals = info.get("values", [])
            single = self._is_single_letter_grade(vals)
            binary = self._is_binary_flag(vals)
            if any(k in low for k in STATUS_KEYWORDS):
                vmap = enum_map.get(f, {})
                if binary:
                    # 布尔故障标记: 0/1 -> 正常/故障
                    status_cn2en.setdefault("正常", "0")
                    status_cn2en.setdefault("故障", "1")
                    for v, cn in vmap.items():
                        cn2 = cn if cn and cn != v else ("正常" if v.strip() in ("0", "false", "False", "否") else "故障")
                        status_cn2en.setdefault(cn2, v)
                else:
                    for v, cn in vmap.items():
                        status_cn2en.setdefault(cn, v)
                continue  # 已归类为状态字段
            if info["is_numeric"]:
                continue
            vmap = enum_map.get(f, {})
            if any(k in low for k in TYPE_KEYWORDS) and not single:
                for v, cn in vmap.items():
                    type_cn2en.setdefault(cn, v)
            elif any(k in low for k in ZONE_KEYWORDS):
                for v, cn in vmap.items():
                    zone_cn2en.setdefault(cn, v)
        return status_cn2en, type_cn2en, zone_cn2en

    def _build_field_aliases(self, field_info):
        """字段别名 + 布尔状态泛化挂到 status。"""
        field_aliases = {}
        for std, patterns in STD_FIELD_PATTERNS.items():
            hits = []
            for f in field_info:
                if any(p in f.lower() for p in patterns):
                    hits.append(f)
                    camel = self._camelize(f)
                    if camel != f and camel not in hits:
                        hits.append(camel)
            if hits:
                field_aliases[std] = hits
        for f, info in field_info.items():
            low = f.lower()
            vals = [str(v) for v in info.get("values", [])[:30]]
            binary = all(v in ("0", "1", "true", "false", "True", "False", "是", "否") for v in vals) if vals else False
            if any(k in low for k in ("failure", "fault", "alarm", "flag", "status", "state")) and binary:
                camel = self._camelize(f)
                field_aliases.setdefault("status", [])
                for alias in (f, camel):
                    if alias not in field_aliases["status"]:
                        field_aliases["status"].append(alias)
        return field_aliases

    def _build_relations_cn2en(self, table):
        """关系词映射: relations.json 的 label → 属性尾名 + 常见同义词。"""
        relations_cn2en = {}
        rel_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "relations.json")
        rel_cfg = {}
        if os.path.exists(rel_file):
            try:
                rel_cfg = json.load(open(rel_file, encoding="utf-8")).get(table, {}).get("object_properties", {})
            except Exception:
                rel_cfg = {}
        for col, cfg in rel_cfg.items():
            rel_en = cfg.get("rel", "").split("#")[-1]
            label = cfg.get("label", "")
            if not (label and rel_en):
                continue
            relations_cn2en[label] = rel_en
            if rel_en == "locatedIn":
                for w in ("位置", "地点", "区域", "车间", "动力站", "仓储区", "物料库", "成品库"):
                    relations_cn2en.setdefault(w, rel_en)
            elif rel_en == "belongsToLine":
                relations_cn2en.setdefault("产线", rel_en)
            elif rel_en == "manufacturedBy":
                for w in ("制造商", "厂商", "厂家"):
                    relations_cn2en.setdefault(w, rel_en)
            elif rel_en == "hasType":
                relations_cn2en.setdefault("类型", rel_en)
        return relations_cn2en

    def _build_entity_cn2en(self, field_info, table):
        """自动生成 entity_cn2en（实体中文词 → 实体类名）。

        从表名/字段名推断实体概念（设备/客户/产品/书/机器/订单/项目/船…），
        value 用表名（下划线形式，对齐本体类名），供 v3 实体总数/列表查询匹配。
        """
        entity_cn2en = {}
        cn = None
        low_t = table.lower()
        for kw, cnw in [("device", "设备"), ("equipment", "设备"), ("machine", "机器"),
                        ("customer", "客户"), ("product", "产品"), ("book", "书"), ("图书", "书"),
                        ("order", "订单"), ("project", "项目"), ("vessel", "船"), ("ship", "船")]:
            if kw in low_t:
                cn = cnw; break
        if not cn:
            for f in field_info:
                fl = f.lower()
                for kw, cnw in [("device", "设备"), ("customer", "客户"), ("product", "产品"),
                                ("book", "书"), ("machine", "机器"), ("name", "记录")]:
                    if kw in fl:
                        cn = cnw; break
                if cn:
                    break
        if cn:
            cls = table  # 表名即实体类名(下划线形式, 对齐本体)
            entity_cn2en[cn] = cls
        return entity_cn2en

    def _build_full_lexicon(self, field_info, attr_map, enum_map, task):
        """组装完整 lexicon.json。"""
        table = task.get("table_name", "数据")
        attr_cn2en, attr_en2cn = self._build_attr_mapping(field_info, attr_map)
        status_cn2en, type_cn2en, zone_cn2en = self._build_enum_mapping(field_info, enum_map)
        field_aliases = self._build_field_aliases(field_info)
        relations_cn2en = self._build_relations_cn2en(table)
        entity_cn2en = self._build_entity_cn2en(field_info, table)
        return {
            "entity_cn2en": entity_cn2en,
            "status_cn2en": status_cn2en,
            "type_cn2en": type_cn2en,
            "zone_cn2en": zone_cn2en,
            "attr_cn2en": attr_cn2en,
            "attr_en2cn": attr_en2cn,
            "field_aliases": field_aliases,
            "relations_cn2en": relations_cn2en,
            "value_fields": {f: attr_en2cn.get(f, f) for f in field_info if f.lower().find("status") >= 0 or f.lower().find("type") >= 0},
            "description": f"由 LexiconAgent 全自动生成 ({table}, {len(field_info)}字段)",
        }

    def main():
        import sys
        _codes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
            "source_csv": os.path.join(_codes, "data", "equipment.csv"),
            "out_lexicon": os.path.join(_codes, "config", "lexicon_auto.json"),
            "use_llm": True, "table_name": "设备",
        }
        r = LexiconAgent().run(task)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
