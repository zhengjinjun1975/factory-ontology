#!/usr/bin/env python3
"""run.py — 现场一站式入口（交付包核心，路径全相对，零硬编码）。

在交付包内任意位置运行，路径基于脚本自身位置自适应。
纯标准库，无第三方依赖（LLM 兜底需 requests + 本地 Ollama，可选）。

用法:
  python run.py setup <数据文件> [表名]   # 自动建模(本体+词典+验证)，支持 csv/json/sqlite/xlsx
  python run.py ask "<问题>"              # 交互问答(规则+LLM兜底)
  python run.py test                       # 自检示例数据
"""

import os
import sys
import json

__version__ = "0.1.3"
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
CFG = os.path.join(ROOT, "config")
STATE = os.path.join(ROOT, "current.json")


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _name(source_path):
    return os.path.splitext(os.path.basename(source_path))[0]


def setup_schema(data_dir, schema_path, table="factory"):
    """schema 驱动统一建模（激进重构核心，替代 csv_to_owl/multi_table）。

    从多表数据目录 + ontology_schema.json 建统一本体（schema_ontology.to_nt），
    输出标准 N-Triples 供下游问答/图检索消费。复用优先·极简落地：
    - 复用 schema_ontology 已验证的 schema 驱动建模内核（sme 精髓）
    - 统一建本体职责到 schema_ontology，消除 csv_to_owl/multi_table 重复

    用法:
      python run.py setup-schema <数据目录> [schema.json] [表名]
    """
    os.makedirs(OUT, exist_ok=True)
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    schema_path = schema_path or os.path.join(CFG, "ontology_schema.json")
    nt = os.path.join(OUT, f"{table}.nt")

    print(f"\n[工厂智能体] schema 驱动建模: {table}")
    # 每步失败都报告清晰原因，不裸抛异常
    try:
        print(f"[1/3] 加载多表数据 {data_dir}")
        data = so.load_all(data_dir)
        print(f"      -> {len(data)} 表: {list(data.keys())}")
    except Exception as e:
        print(f"❌ 本体建模失败（步骤1/3 加载数据）: {e}")
        return None, None

    try:
        if not os.path.exists(schema_path):
            print(f"❌ 本体建模失败（步骤2/3）: 无 schema 文件 {schema_path}，需提供 ontology_schema.json")
            return None, None
        print(f"[2/3] 加载 schema + 约束校验")
        schema = so.load_schema(schema_path)
        issues = so.validate(data, schema)
        if issues:
            print(f"⚠️ 约束校验 {len(issues)} 问题:", [i["msg"] for i in issues[:3]])
    except Exception as e:
        print(f"❌ 本体建模失败（步骤2/3 加载 schema）: {e}")
        return None, None

    try:
        print(f"[3/3] 建统一本体 -> {os.path.basename(nt)}")
        lines = so.to_nt(data, schema, outpath=nt)
        if not os.path.exists(nt):
            raise RuntimeError("to_nt 未生成本体文件")
        graph = so.build_graph(data, schema)
        model = so.build_ontology_model(data, schema)
        print(f"✅ 本体: {len(lines)} 行 N-Triples | 图 {len(graph['nodes'])} 节点/{len(graph['edges'])} 边")
        print(f"   类型体系: {[h['name'] for h in model['type_hierarchy']]}")
        print(f"   语义域: {model['semantic_domains']}")
    except Exception as e:
        print(f"❌ 本体建模失败（步骤3/3 建本体）: {e}")
        return None, None

    json.dump({"nt": os.path.relpath(nt, ROOT), "schema": os.path.relpath(schema_path, ROOT),
               "data_dir": os.path.relpath(data_dir, ROOT), "table": table},
              open(STATE, "w", encoding="utf-8"))
    return nt, None


def setup(source_path, table=None, use_llm=True):
    os.makedirs(OUT, exist_ok=True)
    table = table or _name(source_path)
    nt = os.path.join(OUT, f"{table}.nt")
    lex = os.path.join(CFG, f"lexicon_{table}.json")

    print(f"\n[工厂智能体] 建模: {table}")
    print(f"[1/3] 转本体 {source_path}")
    csv2owl = _load("csv_to_owl", os.path.join(ROOT, "csv_to_owl.py"))
    old = sys.argv
    # 若有对象属性配置则传入 --relations
    rel_path = os.path.join(CFG, "relations.json")
    rel_args = ["--relations", rel_path] if os.path.exists(rel_path) else []
    sys.argv = ["csv_to_owl", source_path, nt] + rel_args
    try:
        csv2owl.main()
    except SystemExit:
        pass
    sys.argv = old
    if not os.path.exists(nt):
        print("❌ 本体生成失败"); return None, None

    print("[2/3] 自动词典 (LLM推断字段语义)...")
    from agents.lexicon_agent import LexiconAgent
    r = LexiconAgent().run({"source_csv": source_path, "out_lexicon": lex,
                            "use_llm": use_llm, "table_name": table})
    if not r.ok:
        print(f"⚠️ 词典: {r.error}")
    if not os.path.exists(lex):
        print("❌ 词典未生成"); return None, None

    # 存状态 + 概要（存相对路径，包可整体迁移，不绑定本机盘符）
    json.dump({"nt": os.path.relpath(nt, ROOT), "lexicon": os.path.relpath(lex, ROOT),
               "table": table}, open(STATE, "w", encoding="utf-8"))
    D = json.load(open(lex, encoding="utf-8"))
    print("[3/3] 建模完成 ✅")
    print("\n=== 词典概要(请人工确认关键字段中文名是否合理) ===")
    for k, v in list(D.get("attr_cn2en", {}).items())[:10]:
        print(f"  {v} = {k}")

    # 本体深度增强：区域层级 + 设备类型父子类（若存在 line.csv）
    try:
        line_csv = os.path.join(ROOT, "data", "line.csv")
        if os.path.exists(line_csv):
            import subprocess
            deep_nt = os.path.join(OUT, f"{table}_deep.nt")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "ontology_depth.py"),
                                source_path, line_csv, nt, deep_nt],
                               capture_output=True, text=True, timeout=120)
            if os.path.exists(deep_nt):
                nt = deep_nt
                json.dump({"nt": os.path.relpath(deep_nt, ROOT), "lexicon": os.path.relpath(lex, ROOT),
                           "table": table}, open(STATE, "w", encoding="utf-8"))
                print(f"🧬 深度增强已应用: {table}_deep.nt")
            else:
                print(f"⚠️ 深度增强失败: {r.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ 深度增强跳过: {e}")
    return nt, lex


def ask(question, nt=None, lex=None):
    if not os.path.exists(STATE):
        print("请先运行: python run.py setup <数据文件>")
        return
    st = json.load(open(STATE, encoding="utf-8"))
    nt = nt or st["nt"]
    lex = lex or st["lexicon"]
    # 兼容相对/绝对路径：相对则基于包根 ROOT 解析（迁移后路径自动重定位）
    nt = nt if os.path.isabs(nt) else os.path.join(ROOT, nt)
    lex = lex if os.path.isabs(lex) else os.path.join(ROOT, lex)
    v3 = _load("ontology_qa_v3", os.path.join(ROOT, "ontology_qa_v3.py"))
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(nt), D)
    if not data:
        print("本体无实例"); return
    ans = v3.answer(question, data, D)
    if ans == "暂不支持该问题":
        # 图检索兜底(与 API 层一致): 开放式/关系问题走 GraphRAG
        from graph_rag import answer_graph
        gans, _ = answer_graph(question, nt)
        print(gans)
    else:
        print(ans)


def self_test():
    """自检: 用示例数据跑通全流程。"""
    data_csv = os.path.join(ROOT, "data", "ai4i.csv")
    if not os.path.exists(data_csv):
        print("无示例数据, 跳过自检")
        return
    print("=== 自检: 示例设备数据 (UCI AI4I 预测性维护) ===")
    nt, lex = setup(data_csv, "ai4i", use_llm=False)
    if nt:
        for q in ["有多少台运行中的设备", "列出所有空压机", "功率最大的设备"]:
            print(f"\n问: {q}")
            ask(q, nt, lex)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    if args[0] == "setup":
        if len(args) < 2:
            print("用法: python run.py setup <数据文件> [表名]"); return
        setup(args[1], args[2] if len(args) > 2 else None)
    elif args[0] == "setup-schema":
        if len(args) < 2:
            print("用法: python run.py setup-schema <数据目录> [schema.json] [表名]"); return
        setup_schema(args[1], args[2] if len(args) > 2 else None,
                     args[3] if len(args) > 3 else "factory")
    elif args[0] == "ask":
        if len(args) < 2:
            print("用法: python run.py ask '<问题>'"); return
        ask(args[1])
    elif args[0] == "test":
        self_test()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
