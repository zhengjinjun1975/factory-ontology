#!/usr/bin/env python3
"""ingest_agent.py — 原子智能体：数据接入 (CSV/数据源 → 本体)。

复用 src/csv_to_owl.py。单一职责：把一份数据文件转成本体文件。
task 结构:
  {"csv_path": "...", "out_nt": "...", "data_source": "csv"}   # 或
  {"raw_rows": [dict,...], "table_name": "Equipment", "out_nt": "..."}  # 内存数据
"""

import os
import sys
import importlib.util

_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
from core.base_agent import BaseAgent, AgentResult

# 加载 csv_to_owl（复用本体生成逻辑，路径基于套件根自适应）
def _load_csv_to_owl():
    spec = importlib.util.spec_from_file_location(
        "csv_to_owl", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "csv_to_owl.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class IngestAgent(BaseAgent):
    name = "ingest"

    def __init__(self):
        super().__init__()
        self._conv = None

    def _default_out(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ingest.nt")

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        if "csv_path" in task:
            return self._from_csv(task)
        elif "raw_rows" in task:
            return self._from_rows(task)
        return self._err("ingest 需要 csv_path 或 raw_rows")

    def _from_csv(self, task):
        csv_path = task["csv_path"]
        out_nt = task.get("out_nt") or self._default_out()
        if not os.path.exists(csv_path):
            return self._err(f"数据文件不存在: {csv_path}")
        if self._conv is None:
            self._conv = _load_csv_to_owl()
        # 复用 csv_to_owl 的 main 逻辑：直接调用其内部转换
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            rows = list(reader)
            headers = reader.fieldnames
        return self._write_ontology(rows, headers, task.get("table_name", "Data"), out_nt, csv_path)

    def _from_rows(self, task):
        rows = task["raw_rows"]
        if not rows:
            return self._err("raw_rows 为空")
        headers = list(rows[0].keys())
        out_nt = task.get("out_nt") or self._default_out()
        return self._write_ontology(rows, headers, task.get("table_name", "Data"), out_nt, None)

    def _write_ontology(self, rows, headers, table, out_nt, src_csv):
        """复用 csv_to_owl 的生成逻辑写本体。"""
        if self._conv is None:
            self._conv = _load_csv_to_owl()
        conv = self._conv
        NS = "http://factory.example/ontology#"
        cls = table.capitalize()
        cls_uri = NS + cls
        prop_types = {}
        for h in headers:
            vals = [r[h] for r in rows if h in r and r[h].strip()]
            prop_types[h] = conv.guess_type(vals[0]) if vals else "xsd:string"

        L = []
        RDFT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        RDFSL = "http://www.w3.org/2000/01/rdf-schema#"
        OWL = "http://www.w3.org/2002/07/owl#"
        XSD = "http://www.w3.org/2001/XMLSchema#"
        L.append(f'<{cls_uri}> <{RDFT}> <{OWL}Class> .')
        L.append(f'<{cls_uri}> <{RDFSL}label> "{cls}" .')
        for h in headers:
            if h.lower() == "id":
                continue
            p = conv.local_name(h)
            L.append(f'<{NS}{p}> <{RDFT}> <{OWL}DatatypeProperty> .')
            L.append(f'<{NS}{p}> <{RDFSL}domain> <{cls_uri}> .')
            L.append(f'<{NS}{p}> <{RDFSL}range> <{XSD}{prop_types[h].split(":")[1]}> .')
        for i, row in enumerate(rows):
            inst_id = row.get("id") or f"{i+1}"
            inst_uri = f"{cls_uri}_{inst_id}"
            L.append(f'<{inst_uri}> <{RDFT}> <{cls_uri}> .')
            for h in headers:
                if h.lower() == "id" or h not in row or not row[h].strip():
                    continue
                p = conv.local_name(h)
                val = row[h].strip()
                t = prop_types[h]
                L.append(f'<{inst_uri}> <{NS}{p}> "{val}"^^<{XSD}{t.split(":")[1]}> .')

        os.makedirs(os.path.dirname(out_nt) or ".", exist_ok=True)
        with open(out_nt, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        return self._ok({"out_nt": out_nt, "rows": len(rows), "class": cls}, "ingest")


def main():
    import json, sys
    _codes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "csv_path": os.path.join(_codes, "data", "equipment.csv"),
        "out_nt": os.path.join(_codes, "output", "via_agent.nt"),
        "table_name": "Equipment",
    }
    r = IngestAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
