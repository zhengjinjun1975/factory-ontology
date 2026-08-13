#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memnote.py — 记忆沉淀(可选, 不侵入主流程): 行业词典/建模经验 note 进 OptMem。

设计定位:
  纯标准库零依赖; 单独运行或按需 import; 主流程不自动调用。
  用于在"关键节点"(如新行业词典建成、建模经验沉淀)时, 把经验固化到
  OptMem 记忆系统(E:\\optmem\\memo note), 便于跨行业、跨会话复用。

用法(命令行):
  python memnote.py note "<一行经验, ≤280字节>"
  python memnote.py lexicon <行业> <中文术语> <英文/规范字段>   # 行业词典经验
  python memnote.py model  <行业> "<建模经验一句话>"
  python memnote.py hint                                          # 打印关键节点提示

用法(代码内, 按需调用):
  import memnote
  memnote.note_lexicon("医药", "灭菌周期", "sterilization_cycle")
  memnote.note_modeling("阀门", "同规格不同压力等级拆分成独立实体避免歧义")

关键节点提示(建议调用的时刻):
  - 新行业词典/同义词组扩展完成  -> note_lexicon()
  - 行业建模/映射经验沉淀       -> note_modeling()
  - 数据接入列映射调通          -> note_modeling(行业, "列映射经验...")

克制原则:
  - 主流程默认不调用, 由使用者按需触发; 失败静默返回, 绝不打断业务。
  - 可用环境变量 OPTMEM_NOTE=0 关闭(默认开)。
"""
from __future__ import annotations

import os
import subprocess
import sys

# ---- 可覆盖配置(均可用环境变量改) -------------------------------------
_MEMO = os.environ.get("OPTMEM_MEMO", r"E:\optmem\memo")
_MEMORY_DIR = os.environ.get("OPTMEM_MEMORY_DIR", r"E:\optmem\memory")
_ENABLED = os.environ.get("OPTMEM_NOTE", "1").lower() not in ("0", "false", "no", "off")
_MAX_BYTES = 280


def _clip(text: str, max_bytes: int = _MAX_BYTES) -> str:
    """按字节截断到 ≤max_bytes, 保证 memo 工具可写入。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    cut = text.encode("utf-8")[:max_bytes]
    return cut.decode("utf-8", errors="ignore")


def note(text: str):
    """沉淀一条记忆进 OptMem。返回 (ok, 消息); 失败不抛异常。"""
    if not _ENABLED:
        return False, "disabled(OPTMEM_NOTE=0)"
    line = _clip(text)
    env = dict(os.environ)
    env["MEMORY_DIR"] = _MEMORY_DIR
    try:
        r = subprocess.run(
            [sys.executable, _MEMO, "note", line],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        if r.returncode == 0:
            first = (r.stdout or r.stderr or "").strip().splitlines()
            return True, (first[0] if first else "ok")
        return False, (r.stderr or r.stdout or "").strip()
    except Exception as e:  # 克制: 任何异常都不向外抛
        return False, f"optmem 不可用: {e}"


# ---- 行业词典经验 -------------------------------------------------------
def note_lexicon(industry: str, term_cn: str, term_en: str, hint: str = ""):
    """记录一条行业词典经验: 中文术语 -> 规范英文/字段。便于跨行业复用。"""
    extra = f" ({hint})" if hint else ""
    return note(f"行业词典经验[{industry}]: 术语『{term_cn}』→ {term_en}{extra}")


# ---- 行业建模经验 -------------------------------------------------------
def note_modeling(industry: str, experience: str):
    """记录一条行业建模经验(映射/实体拆分/词典注意点), 供其他行业复用。"""
    return note(f"行业建模经验[{industry}]: {experience}")


def hint():
    """打印关键节点提示: 何时、如何把经验沉淀进 OptMem。"""
    print("关键节点提示 —— 用 memnote 把经验沉淀进 OptMem:")
    print("  · 新行业词典/同义词组建成后: python memnote.py lexicon <行业> <中文术语> <英文>")
    print("  · 建模/映射经验:            python memnote.py model <行业> \"<经验一句话>\"")
    print("  · 或直接 note:              python memnote.py note \"<一行经验>\"")
    print("沉淀后可用 python E:\\optmem\\memo_search.py \"<关键词>\" 跨行业复用。")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] == "hint":
        hint()
        return 0
    cmd = argv[0]
    if cmd == "note" and len(argv) >= 2:
        ok, msg = note(argv[1])
        print(msg)
        return 0 if ok else 1
    if cmd == "lexicon" and len(argv) >= 4:
        ok, msg = note_lexicon(argv[1], argv[2], argv[3])
        print(msg)
        return 0 if ok else 1
    if cmd == "model" and len(argv) >= 3:
        ok, msg = note_modeling(argv[1], argv[2])
        print(msg)
        return 0 if ok else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
