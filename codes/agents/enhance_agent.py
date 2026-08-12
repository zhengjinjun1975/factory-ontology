#!/usr/bin/env python3
"""enhance_agent.py — 原子智能体：语义补全问答词典。

核心：给一个已有词典(lexicon.json)，用 LLM 补全词典缺项——为关键属性/状态/类型词
生成中文同义词(如 功率→额定功率/装机功率, 运行→运行中/正常/运行状态)，
并把同义词合并回词典，提升用户自然语言查询的召回率。

职责(单一)：
1. 读词典(attr_cn2en / status_cn2en / type_cn2en / zone_cn2en 等)
2. 用 llm_generate 让模型为每个中文词条补全同义词(限定工业语义域)
3. 去重合并回原词典，写回(或返回增强后的词典数据)
4. 返回 AgentResult: data={enhanced_entries, new_synonyms, out_lexicon}

task 结构:
  {"lexicon_path": "...", "table_name": "设备", "use_llm": true, "out_lexicon": "..."}
- use_llm=False 时降级：返回原词典，标注未增强。
"""

import os
import sys
import json
import re

# 路径修正（与 lexicon_agent.py 保持一致）
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
sys.path.insert(0, os.path.join(_APP, "..", "src"))

from core.base_agent import BaseAgent, AgentResult

# 统一模型调用（从 model_config.json 读取，可切 ornith/deepseek）
from model_llm import llm_generate


# 需要补全同义词的词典键（含中文→英文映射的语义词表）
SYNONYM_TARGET_KEYS = ("attr_cn2en", "status_cn2en", "type_cn2en", "zone_cn2en")
# 只透传、不做同义词扩展的键（双向索引/别名/描述，避免语义膨胀失真）
PASSTHROUGH_KEYS = ("attr_en2cn", "field_aliases", "value_fields", "description")


class EnhanceAgent(BaseAgent):
    name = "enhance"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        lex_path = task.get("lexicon_path")
        if not lex_path or not os.path.exists(lex_path):
            return self._err(f"词典文件不存在: {lex_path}")

        try:
            with open(lex_path, encoding="utf-8") as f:
                lexicon = json.load(f)
        except Exception as e:
            return self._err(f"词典解析失败: {e}")
        if not isinstance(lexicon, dict):
            return self._err(f"词典结构非法(应为 dict): {type(lexicon).__name__}")

        use_llm = task.get("use_llm", True)
        if not use_llm:
            # 降级：不调模型，返回原词典并标注未增强
            return self._ok({
                "enhanced": False,
                "reason": "use_llm=False，未调用模型",
                "out_lexicon": lex_path,
                "new_synonyms": 0,
                "lexicon": lexicon,
            }, "enhance")

        table = task.get("table_name", "词典")
        # 1. 对每个中文语义词表生成同义词
        new_synonyms = {}   # {key: {原中文词: [新增同义词,...]}}
        for key in SYNONYM_TARGET_KEYS:
            src = lexicon.get(key, {})
            if not isinstance(src, dict) or not src:
                continue
            adds = self._llm_synonyms(src, key, table)
            if not adds:
                continue
            merged = self._merge_synonyms(src, adds)
            lexicon[key] = merged["merged"]
            new_synonyms[key] = merged["added"]

        # 2. 统计 + 写回
        total_new = sum(len(v) for v in new_synonyms.values())
        out = task.get("out_lexicon") or lex_path
        with open(out, "w", encoding="utf-8") as f:
            json.dump(lexicon, f, ensure_ascii=False, indent=2)

        return self._ok({
            "enhanced": True,
            "out_lexicon": out,
            "table_name": table,
            "new_synonyms": total_new,
            "enhanced_entries": {k: len(v) for k, v in new_synonyms.items()},
            "new_synonyms_detail": new_synonyms,
            "lexicon": lexicon,
        }, "enhance")

    # ---------------- 同义词生成(LLM) ----------------

    def _llm_synonyms(self, cn2en: dict, key: str, table: str):
        """让 LLM 为词表每个中文词条补全 1~3 个工业同义/近义表达。
        返回 {中文词: [同义词,...]} 或空 dict。"""
        if len(cn2en) > 40:
            # 词条过多时分批，避免 prompt 过长
            items = list(cn2en.items())
            result = {}
            for i in range(0, len(items), 40):
                result.update(self._llm_synonyms_batch(dict(items[i:i + 40]), key, table))
            return result
        return self._llm_synonyms_batch(cn2en, key, table)

    def _llm_synonyms_batch(self, cn2en: dict, key: str, table: str):
        if not cn2en:
            return {}
        kind = {"attr_cn2en": "属性/测量指标", "status_cn2en": "设备状态",
                "type_cn2en": "设备类型", "zone_cn2en": "区域/位置"}.get(key, "词条")
        desc = "\n".join(f"- {cn}: {en}" for cn, en in sorted(cn2en.items(), key=lambda x: len(x[0]), reverse=True))
        prompt = (
            f"你是工厂设备领域的术语专家。下面'{table}'{kind}的中文词表(中文名→英文)。\n"
            "请为【每一个中文词条】补充 1~3 个在工业场景中常见、可互换的中文同义词/近义口语表达"
            "(如 功率→额定功率/装机功率, 运行→运行中/正常/运行状态, 故障→报警/异常/停机)。\n"
            "要求:\n"
            "- 只补充语义相近、真实可互换的词，宁缺毋滥；不要给无关词\n"
            "- 不要重复词表里已出现的词\n"
            "- 严格返回 JSON，不要额外文字\n\n"
            f"【词表】\n{desc}\n\n"
            '【返回】{"synonyms": {"原中文词": ["同义词1", "同义词2", ...], ...}}'
        )
        text = llm_generate(prompt, temperature=0.2, max_tokens=2500)
        if not text or text.startswith("[模型"):
            return {}
        text = re.sub(r'```\w*\s*|\s*```', '', text).strip()
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return {}
        try:
            return json.loads(text[s:e + 1]).get("synonyms", {})
        except Exception:
            return {}

    # ---------------- 合并 ----------------

    def _merge_synonyms(self, cn2en: dict, adds: dict):
        """把 LLM 同义词合并进 cn2en：原词条值保留，同义词映射到同一英文值。
        返回 {"merged": 新词表, "added": {原词: [实际新增同义词]}}。"""
        merged = dict(cn2en)
        added = {}
        for cn, syns in adds.items():
            if not isinstance(syns, list) or cn not in cn2en:
                continue
            en = cn2en[cn]
            new = []
            for s in syns:
                s = str(s).strip()
                if not s or s == cn:
                    continue
                if s in merged:           # 已存在(含其他词条)，跳过
                    continue
                merged[s] = en            # 同义词 → 同一英文值
                new.append(s)
            if new:
                added[cn] = new
        return {"merged": merged, "added": added}

    def main(self):
        import sys as _s
        _codes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        task = json.loads(_s.argv[1]) if len(_s.argv) > 1 else {
            "lexicon_path": os.path.join(_codes, "config", "lexicon.json"),
            "table_name": "设备", "use_llm": True,
        }
        r = EnhanceAgent().run(task)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    EnhanceAgent().main()
