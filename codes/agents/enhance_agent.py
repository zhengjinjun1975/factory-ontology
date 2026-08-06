#!/usr/bin/env python3
"""enhance_agent.py — 原子智能体：本体语义补全。

对本体做规则/LLM 语义补全（类型层级、状态优先级、关系推断）。
task 结构:
  {"nt_in": "...", "nt_out": "...", "use_llm": true}

v2 修复：
- 移除硬编码 本仓库 绝对路径（违反"零依赖可迁移"宣称）
- 移除对不存在的 ontology_enhance.py 依赖，改用套件自己的 ontology_qa_v3.parse_nt/build_data
"""

import os
import sys
import importlib.util

_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))

from core.base_agent import BaseAgent, AgentResult


def _load_v3():
    v3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ontology_qa_v3.py")
    spec = importlib.util.spec_from_file_location("ontology_qa_v3", v3_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class EnhanceAgent(BaseAgent):
    name = "enhance"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        codes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        nt_in = task.get("nt_in") or os.path.join(codes_root, "output", "equipment.nt")
        if not os.path.isabs(nt_in):
            nt_in = os.path.join(codes_root, nt_in)
        nt_out = task.get("nt_out") or nt_in.replace(".nt", "_enhanced.nt")
        if not os.path.isabs(nt_out):
            nt_out = os.path.join(codes_root, nt_out)
        if not os.path.exists(nt_in):
            return self._err(f"本体不存在: {nt_in}")

        v3 = _load_v3()
        triples = v3.parse_nt(nt_in)
        data = v3.build_data(triples, {})
        if not data:
            return self._err(f"本体解析失败或空: {nt_in}")

        # 规则回退：收集分布
        types = sorted(set(d.get("deviceType", "?") for d in data.values()))
        mappings = {
            "mappings": [
                {"from": t, "to": "生产设备", "type": "subClassOf", "property": "", "reason": "规则回退"}
                for t in types if t in ("cnc_machining", "injection_molding", "welding_robot", "assembly_line", "inspection_cam")
            ] + [
                {"from": t, "to": "动力设备", "type": "subClassOf", "property": "", "reason": "规则回退"}
                for t in types if t in ("air_compressor", "power_dist", "cooling_unit")
            ]
        }

        L = []
        for m in mappings.get("mappings", []):
            if m.get("type") == "subClassOf":
                cat = m["to"].replace(" ", "")
                L.append(f'<http://factory.example/ontology#{cat}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> .')
                L.append(f'<http://factory.example/ontology#{m["from"]}> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <http://factory.example/ontology#{cat}> .')
        pri = {"alarm": "P0", "maintenance": "P1", "idle": "P2", "running": "P3", "offline": "P1"}
        L.append(f'<http://factory.example/ontology#hasPriority> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#ObjectProperty> .')
        for st, pr in pri.items():
            L.append(f'<http://factory.example/ontology#hasPriority_{st}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#NamedIndividual> .')
            L.append(f'<http://factory.example/ontology#hasPriority_{st}> <http://www.w3.org/2000/01/rdf-schema#label> "{pr}" .')

        os.makedirs(os.path.dirname(nt_out) or ".", exist_ok=True)
        with open(nt_out, "w", encoding="utf-8") as f:
            with open(nt_in, encoding="utf-8") as fi:
                f.write(fi.read())
            f.write("\n# -- 原子智能体 enhance 补全 --\n")
            f.write("\n".join(L) + "\n")
        return self._ok({"nt_out": nt_out, "subclass": sum(1 for m in mappings["mappings"] if m.get("type") == "subClassOf"),
                         "used_llm": False}, "enhance")


def main():
    import json, sys
    _codes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "nt_in": os.path.join(_codes, "output", "equipment.nt"),
        "nt_out": os.path.join(_codes, "output", "equipment_enhanced.nt"),
        "use_llm": False,
    }
    r = EnhanceAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
