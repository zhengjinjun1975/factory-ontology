#!/usr/bin/env python3
"""pipeline.py — 加工循环编排器：串联 Ingest→Lexicon→Enhance→Query。

把四个原子智能体串成一条“数据→本体→词典→问答”的加工循环，对外暴露
单一 run(task) 契约，让上层(CLI/API/测试)不必关心各环节如何衔接。
本编排器只负责串联与结果汇总，不掺入任何业务建模逻辑。

流程：
1. IngestAgent   数据接入 → 本体 .nt
2. LexiconAgent  生成问答词典
3. (可选) EnhanceAgent  语义补全(use_llm=True 时执行)
4. QueryAgent    用几个示例问题验证建模是否成功

task 结构:
  {"source_path": "...", "table_name": "...", "schema": "...", "use_llm": true}
  - source_path: 数据文件(单文件建模) 或 数据目录(schema 驱动建模) —— 必填
  - table_name:  可选，本体/词典的类名；缺省各智能体自行推断
  - schema:      可选，ontology_schema.json 路径；给则 Ingest 走 schema 驱动建模
  - use_llm:     可选，是否启用词典语义补全，缺省 True

返回 AgentResult:
  ok=True, data={
      "nt_path": ...,
      "lexicon_path": ...,
      "enhanced": bool,
      "verify_results": [{"question":..., "answer":..., "engines":[...]}, ...],
      "steps": [...],   # 各环节是否成功
  }
任一环节失败即中止，返回该环节的失败信息。
"""

import os
import sys

# 路径修正(与各 agent 一致)：让 agents / core / src 都进 path
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, "src"))

from core.base_agent import BaseAgent, AgentResult

# 规则引擎兜底哨兵(与 run.py/query_agent/eval_agent 一致)
_MISS_SENTINEL = "暂不支持该问题"

# 建模成功验证用的示例问题(与数据解耦, 命中率仅供人工参考)
DEFAULT_VERIFY_QUESTIONS = [
    "有多少台运行中的设备",
    "设备总数是多少",
    "列出所有设备的类型",
]


class Pipeline(BaseAgent):
    """加工循环编排器：串行调度四个原子智能体。"""

    name = "pipeline"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        src = task.get("source_path")
        if not src or not os.path.exists(src):
            return self._err(f"数据源不存在: {src}")

        use_llm = task.get("use_llm", True)
        table = task.get("table_name")

        # ---- 1. IngestAgent: 数据接入 → 本体 .nt ----
        from agents.ingest_agent import IngestAgent
        r = IngestAgent().run({
            "source_path": src,
            "table_name": table,
            "schema": task.get("schema"),
        })
        if not r.ok:
            return self._step_fail("ingest", r)
        nt_path = (r.data or {}).get("nt_path")
        print(f"✅ [pipeline] ingest → {nt_path}")

        # ---- 2. LexiconAgent: 生成问答词典 ----
        from agents.lexicon_agent import LexiconAgent
        r = LexiconAgent().run({
            "source_csv": src,
            "table_name": table,
            "use_llm": use_llm,
        })
        if not r.ok:
            return self._step_fail("lexicon", r)
        lexicon_path = (r.data or {}).get("out_lexicon")
        print(f"✅ [pipeline] lexicon → {lexicon_path}")

        # ---- 3. (可选) EnhanceAgent: 语义补全 ----
        enhanced = False
        if use_llm:
            from agents.enhance_agent import EnhanceAgent
            r = EnhanceAgent().run({
                "lexicon_path": lexicon_path,
                "table_name": table,
                "use_llm": True,
            })
            if not r.ok:
                return self._step_fail("enhance", r)
            lexicon_path = (r.data or {}).get("out_lexicon") or lexicon_path
            enhanced = bool((r.data or {}).get("enhanced"))
            print(f"✅ [pipeline] enhance → {lexicon_path} (enhanced={enhanced})")
        else:
            print("ℹ️ [pipeline] use_llm=False, 跳过语义补全")

        # ---- 4. QueryAgent: 示例问题验证建模成功 ----
        from agents.query_agent import QueryAgent
        qa = QueryAgent()
        verify = []
        for q in (task.get("verify_questions") or DEFAULT_VERIFY_QUESTIONS):
            r = qa.run({"question": q, "nt_file": nt_path,
                        "lexicon": lexicon_path, "use_llm": False})
            ans = (r.data or {}).get("answer", "") if r.ok else ""
            verify.append({
                "question": q,
                "answer": ans,
                "engines": (r.data or {}).get("engines", []) if r.ok else [],
                "hit": bool(ans) and ans != _MISS_SENTINEL,
            })
            hit = "✓" if verify[-1]["hit"] else "✗"
            print(f"  [pipeline] verify {hit} {q} → {ans[:40]}")

        return self._ok({
            "nt_path": nt_path,
            "lexicon_path": lexicon_path,
            "enhanced": enhanced,
            "verify_results": verify,
            "verify_hits": sum(1 for v in verify if v["hit"]),
            "verify_total": len(verify),
            "steps": ["ingest", "lexicon"] + (["enhance"] if use_llm else []) + ["query"],
        }, "pipeline")

    # ---------------- 工具 ----------------

    def _step_fail(self, step, r: AgentResult) -> AgentResult:
        """某环节失败：包装成 pipeline 整体失败并清晰报告原因。"""
        print(f"❌ [pipeline] 环节 {step} 失败: {r.error}")
        return AgentResult(ok=False, data={"failed_step": step,
                                           "step_error": r.error},
                           error=f"[pipeline] 环节 {step} 失败: {r.error}",
                           agent=self.name)

    def main(self):
        import json
        task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
            "source_path": os.path.join(_APP, "data", "equipment.csv"),
            "table_name": "设备",
            "use_llm": True,
        }
        r = self.run(task)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    Pipeline().main()
