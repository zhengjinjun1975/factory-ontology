"""一把跑全部自检门 —— 收口用（也可给 CI 调用）。

用法：
    C:/Python312/python.exe scripts/run_all_gates.py            # 全部
    C:/Python312/python.exe scripts/run_all_gates.py --fast     # 跳过最慢的评测基线

退出码：0 = 全绿；1 = 有门不过（并打印是哪一个）。
纯标准库；不启常驻服务（各门自己起临时端口并收尾）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python"

GATES = [
    ("KB 注册表与词典分层", ["scripts/verify_kb_registry.py"]),
    ("商用加固（鉴权/方言/审计链）", ["scripts/verify_hardening.py"]),
    ("编排上半截（流程/控制流/预设）", ["scripts/verify_flows.py"]),
    ("问答信封与未命中", ["scripts/verify_qa_envelope.py"]),
    ("模型建议层", ["scripts/verify_advisory.py"]),
    ("边界自检（甲方痕迹/依赖）", ["scripts/check_boundary.py"]),
    ("仓库单测", ["-m", "pytest", "codes/tests", "-q"]),
]
SLOW_GATES = [
    ("问答评测基线（回归门）", ["scripts/eval_qa.py"]),
]


def run(name, args):
    cmd = [PY] + args
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    hits = [ln.strip() for ln in tail
            if any(k in ln for k in ("自检结果", "通过", "结论", "结果:", "passed", "failed", "违规"))]
    summary = hits[-1] if hits else (tail[-1] if tail else "(无输出)")
    return p.returncode, summary


def main():
    fast = "--fast" in sys.argv
    gates = GATES + ([] if fast else SLOW_GATES)
    bad = []
    print("=" * 74)
    print("全部门一览  仓库: %s" % ROOT)
    print("=" * 74)
    for name, args in gates:
        rc, summary = run(name, args)
        flag = "PASS" if rc == 0 else "FAIL"
        print("  [%s] %-28s rc=%d  %s" % (flag, name, rc, summary[:88]))
        if rc != 0:
            bad.append(name)
    print("-" * 74)
    if bad:
        print("不全绿：%d 个门未过 -> %s" % (len(bad), bad))
    else:
        print("全部通过（%d 个门）" % len(gates))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
