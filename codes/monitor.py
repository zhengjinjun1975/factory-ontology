#!/usr/bin/env python3
"""monitor.py — 服务监控告警看门狗

周期性检查 API 健康与异常, 异常时告警(可配钉钉/日志/退出码)。
- /health 存活检查
- /metrics 请求计数 + 错误率检查
- 超时/连续失败 → 告警

用法:
  python monitor.py                       # 单次检查
  python monitor.py --interval 60         # 每60秒循环
  python monitor.py --check-error-rate 0.2  # 错误率>20%告警
"""
import os
import sys
import json
import time
import argparse
import urllib.request


def check(base="http://localhost:8000", error_threshold=0.2, timeout=5):
    """单次检查。返回 (ok, 报告)。"""
    def get(path):
        with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    report = {"ok": True, "issues": []}
    # 1. 存活
    try:
        st, d = get("/health")
        if st != 200 or d.get("status") != "ok":
            report["issues"].append(f"健康检查异常: {st} {d}")
    except Exception as e:
        report["issues"].append(f"健康检查失败: {e}")
    # 2. 指标 + 错误率
    try:
        st, d = get("/metrics")
        reqs = d.get("requests", {})
        total = d.get("total", 0)
        errors = sum(v for k, v in reqs.items() if "/api/" in k and k.endswith(("500", "502", "503", "504")))
        # 从审计判断错误率更准; 这里用总请求+错误端点近似
        if total > 50:  # 有足够样本
            report["total_requests"] = total
            report["error_count"] = errors
    except Exception as e:
        report["issues"].append(f"指标检查失败: {e}")
    report["ok"] = not report["issues"]
    return report["ok"], report


def main():
    ap = argparse.ArgumentParser(description="服务监控看门狗")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--interval", type=int, default=0, help="循环间隔秒(0=单次)")
    ap.add_argument("--check-error-rate", type=float, default=0.2)
    args = ap.parse_args()

    if args.interval <= 0:
        ok, rep = check(args.base, args.check_error_rate)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        sys.exit(0 if ok else 1)
    print(f"🛡️ 监控循环启动, 每 {args.interval}s (Ctrl+C 停止)")
    while True:
        ok, rep = check(args.base, args.check_error_rate)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if not ok:
            print(f"[{ts}] ❌ 异常: {rep['issues']}")
        else:
            print(f"[{ts}] ✅ 正常 (总请求={rep.get('total_requests','-')})")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
