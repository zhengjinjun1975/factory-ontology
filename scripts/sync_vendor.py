#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_vendor.py — factory-ontology → 各库 vendor/ 的代码级同步工具。

纪律(用户明确): 同步不跨库调用, 每库靠"拷贝"完全独立存在, 是真正的代码级同步。
- factory-ontology 是共享内核的**权威源(single source of truth)**;
- 各消费者库(opa-monitor 等)的 vendor/ 是其**镜像拷贝**, 库内独立运行, 不 import 本库;
- 本脚本把权威源文件拷到各库 vendor/, 并校验 MD5 一致, 防止漂移。

用法:
  python sync_vendor.py                    # 同步所有已注册的消费者库(默认, 无则列出)
  python sync_vendor.py --verify-only      # 只校验各库 vendor 与源是否一致, 不改写
  python sync_vendor.py --list             # 列出权威源文件 + 注册的消费者库

新增共享内核文件: 把文件加进 KERNEL_FILES; 新增消费者库: 把库根路径加进 CONSUMERS。
创建日期: 2026-09-03
"""
import os
import sys
import hashlib
import shutil

# ── 权威源: 共享建模内核(单一事实来源, 三库收敛后唯一引擎) ──
# 多源查找(按序优先): ① ontology-core(共享内核收敛点, 单一事实来源)
#                    ② 本仓库 codes/(完整内核闭包: csv_to_owl/data_loader/model_llm/export)
# 支持环境变量 ONTOLOGY_CORE_DIR 覆盖第一源。
_CORE = os.environ.get("ONTOLOGY_CORE_DIR") or r"D:\ontology-core"
SRC_DIRS = [_CORE, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codes"))]


def find_src(fname):
    """按序在权威源目录里找内核文件, 返回首个存在的绝对路径(找不到返回 None)。"""
    for d in SRC_DIRS:
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    return None
KERNEL_FILES = [
    "csv_to_owl.py",       # CSV→N-Triples 建模(含 --relations 对象属性支持)
    "data_loader.py",      # 多源数据加载(纯标准库)
    "schema_ontology.py",  # 二代建模引擎: 标准字段/命名空间/IRI/to_nt/词典
    "ontology_export.py",  # 合规导出层: OWL/SHACL/JSON-LD(纯标准库, 离线单向)
    "ontology_import.py",  # 本体导入层: Turtle/JSON-LD 解析 + 往返自检 + 外部对齐(纯标准库)
    "ontology_quality.py",  # 建模质量门: label/定义/关系/结构 体检 + 阈值判定(纯标准库)
    "model_llm.py",        # schema_ontology 的 LLM 增强依赖(惰性 import, 舆情已装 requests)
]

# ── 镜像目标: (库根, 目标子目录) —— 内核镜像到各库的该目录 ──
# 注意: 本仓库 codes/ 也是镜像目标 —— 母体(ontology-core)是唯一事实来源,
# 若只同步到外部消费者而漏掉本仓库, 母体的修正就不会落到 factory 自己的内核副本(会漂移)。
CONSUMERS = [
    (r"D:\factory-ontology", "codes"),                    # 本仓库内核副本(从母体回写)
    (r"D:\opa-monitor", "vendor"),                        # 本体驱动舆情监控
    (r"D:\sme-decision-ontology", "vendor"),              # 本体决策域
    (r"D:\sme-decision-ontology\codes\opa_mod", "vendor"),  # 本体决策域·舆情子模块(vendor-first 自包含镜像)
]


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    args = sys.argv[1:]
    verify_only = "--verify-only" in args
    do_list = "--list" in args

    if do_list:
        print("权威源目录(按优先级):")
        for d in SRC_DIRS:
            print(f"  {d}  {'(存在)' if os.path.isdir(d) else '(不存在)'}")
        print("共享内核文件:")
        for f in KERNEL_FILES:
            p = find_src(f)
            print(f"  {f}  [{md5(p) if p else '缺失'}]  {os.path.dirname(p) if p else '缺失!'}")
        print("消费者库(vendor/ 镜像):")
        for c, sub in CONSUMERS:
            print(f"  {os.path.join(c, sub)}")
        return

    print("权威源(按优先级): " + " | ".join(SRC_DIRS))
    print(f"同步模式: {'仅校验(不写入)' if verify_only else '拷贝同步'}")
    changed = 0
    for c, sub in CONSUMERS:
        vendor = os.path.join(c, sub)
        print(f"\n── 消费者: {os.path.basename(c)}/{sub}  →  {vendor}")
        if not os.path.isdir(vendor):
            os.makedirs(vendor, exist_ok=True)
        for f in KERNEL_FILES:
            sp = find_src(f)
            dp = os.path.join(vendor, f)
            if not sp:
                print(f"  [SKIP] 权威源缺失: {f}")
                continue
            src_md5 = md5(sp)
            if os.path.exists(dp):
                dst_md5 = md5(dp)
                if src_md5 == dst_md5:
                    print(f"  [OK]   {f} 已同步(一致)")
                    continue
                if verify_only:
                    print(f"  [DIFF] {f} 源/库不一致! 源={src_md5[:8]} 库={dst_md5[:8]}")
                    changed += 1
                    continue
            shutil.copy2(sp, dp)
            print(f"  [SYNC] {f} 已拷贝 (md5 {src_md5[:8]})")
            changed += 1

    if verify_only:
        print(f"\n校验完成: {changed} 个文件不一致" if changed else "\n校验完成: 全部一致, 无漂移 ✅")
    else:
        print(f"\n同步完成: {changed} 个文件已更新" if changed else "\n同步完成: 全部已是最新 ✅")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
