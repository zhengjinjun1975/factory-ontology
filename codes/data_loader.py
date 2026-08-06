#!/usr/bin/env python3
"""data_loader.py — 统一数据读取：CSV / JSON / SQLite / Excel

为 csv_to_owl / multi_table 提供统一的 load_table(path)，返回 (表名, 列名, 行)。
- CSV / JSON / SQLite 用标准库（零依赖）
- Excel(.xlsx/.xls) 可选，需装 openpyxl（未装时给出清晰提示）

用法:
  from data_loader import load_table
  name, headers, rows = load_table("data/equipment.xlsx")

支持格式:
  .csv            逗号分隔
  .json           数组[{...}] 或 {"rows":[...]} / {"data":[...]}
  .db/.sqlite/.sqlite3   SQLite 库(取第一个表)
  .xlsx/.xls       需 openpyxl
"""
import os
import csv
import json


def load_table(path):
    """统一加载表。返回 (表名, 列名 list, 行 list[dict])。行值统一转 str（与 CSV 一致）。"""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.splitext(os.path.basename(path))[0]

    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            headers = list(reader.fieldnames) if reader.fieldnames else []
        return name, headers, rows

    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else data.get("rows", data.get("data", []))
        if not rows or not isinstance(rows[0], dict):
            raise ValueError(f"JSON 无数据行(需 list[dict] 或 {{rows:[...]}}): {path}")
        headers = list(rows[0].keys())
        rows = [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in rows]
        return name, headers, rows

    if ext in (".db", ".sqlite", ".sqlite3"):
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if not tabs:
                raise ValueError(f"SQLite 无表: {path}")
            table = tabs[0]
            cur = conn.execute(f'SELECT * FROM "{table}"')
            headers = [d[0] for d in cur.description]
            rows = [dict(zip(headers, ["" if x is None else str(x) for x in row])) for row in cur.fetchall()]
        finally:
            conn.close()
        return name, headers, rows

    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError:
            raise ImportError("Excel 支持需安装 openpyxl: pip install openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            header_row = next(it, None)
            if header_row is None:
                raise ValueError(f"Excel 为空: {path}")
            headers = [str(h) if h is not None else f"col{i}" for i, h in enumerate(header_row)]
            rows = [dict(zip(headers, ["" if v is None else str(v) for v in r])) for r in it]
        finally:
            wb.close()
        return name, headers, rows

    raise ValueError(f"不支持的数据格式: {ext}（支持 csv/json/sqlite/xlsx）")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python data_loader.py <数据文件>")
        sys.exit(1)
    n, h, rows = load_table(sys.argv[1])
    print(f"表: {n} | 列: {len(h)} | 行: {len(rows)}")
    print("列名:", ", ".join(h))
