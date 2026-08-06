#!/usr/bin/env python3
"""pipeline.py — 通用原子智能体编排器（零依赖，可打包部署到工厂现场）。

编排器协调原子智能体，支持三类任务：
  type=ingest   数据接入: CSV/内存数据 -> 本体
  type=query    本体问答: 问题 -> 答案 (规则优先+LLM兜底)
  type=enhance  语义补全: 本体 -> 补全后本体
  type=ops      运维分析: 基于本体的异常/维护/统计报告

每个原子智能体实现 BaseAgent.run(task)->AgentResult，编排器按 type 路由。
用法:
  python pipeline.py <task.json>    # 单任务
  python pipeline.py ingest "D:/...csv"        # 便捷: 数据接入
  python pipeline.py query "有多少台运行中"     # 便捷: 问答
  python pipeline.py ops --nt xxx.nt           # 运维分析
"""

import sys
import os
import json
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from core.base_agent import AgentResult
from agents.ingest_agent import IngestAgent
from agents.enhance_agent import EnhanceAgent
from agents.query_agent import QueryAgent
from agents.ops_agent import OpsAgent  # noqa: F401
from agents.lexicon_agent import LexiconAgent  # noqa: F401

# 默认本体：基于套件根目录自适应（可迁移，不绑定本机盘符）
DEFAULT_NT = os.path.join(APP_DIR, "output", "equipment.nt")
DEFAULT_OUT_NT = os.path.join(APP_DIR, "output", "via_agent.nt")

# 原子智能体注册表（可扩展：加新智能体只需在此注册）
REGISTRY = {
    "ingest": IngestAgent,
    "enhance": EnhanceAgent,
    "query": QueryAgent,
    "ops": OpsAgent,
    "lexicon": LexiconAgent,
}


def run_task(task: dict) -> AgentResult:
    """编排器主入口：按 type 路由到对应原子智能体。"""
    ttype = task.get("type", "query")
    agent_cls = REGISTRY.get(ttype)
    if not agent_cls:
        return AgentResult(ok=False, error=f"未知任务类型: {ttype} (可用: {list(REGISTRY)})", agent="orchestrator")
    agent = agent_cls()
    result = agent.run(task)
    result.agent = f"orchestrator/{agent.name}"
    return result


def cli():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    # 便捷模式
    if args[0] in ("ingest", "query", "enhance", "ops"):
        ttype = args[0]
        if ttype == "ingest" and len(args) >= 2:
            task = {"type": "ingest", "csv_path": args[1],
                    "out_nt": args[2] if len(args) > 2 else DEFAULT_OUT_NT,
                    "table_name": args[3] if len(args) > 3 else "Data"}
        elif ttype == "query" and len(args) >= 2:
            task = {"type": "query", "question": args[1],
                    "nt_file": args[2] if len(args) > 2 else DEFAULT_NT,
                    "use_llm": False}
        elif ttype == "enhance":
            task = {"type": "enhance", "nt_in": args[1] if len(args) > 1 else DEFAULT_NT,
                    "nt_out": args[2] if len(args) > 2 else DEFAULT_NT.replace(".nt", "_enhanced.nt"),
                    "use_llm": False}
        else:  # ops
            task = {"type": "ops", "nt_file": args[1] if len(args) > 1 else DEFAULT_NT}
    else:
        # JSON 任务
        if args[0].endswith(".json"):
            with open(args[0], encoding="utf-8") as f:
                task = json.load(f)
        else:
            task = json.loads(args[0])

    t0 = time.time()
    r = run_task(task)
    elapsed = time.time() - t0
    out = r.to_dict()
    out["total_s"] = round(elapsed, 3)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    cli()
