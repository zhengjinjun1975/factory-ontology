#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_chainbreak.py — 前端 ↔ 后端「断链审计」(纯标准库, 零第三方依赖)

审计目标
--------
`codes/api_server.py` 暴露的每个后端路由, 前端(web/)是否有入口。判定**只依据
仓库里真实存在的静态引用**, 不做任何「按理应该有」的猜测。

入口从哪读(先看 web/ 目录结构, 弄清三层)
---------------------------------------
  1) 页面层 : web/*.html(admin.html/index.html)、web/food_app/*  —— 直接 fetch 后端
  2) SPA 层 : web/src/**  (源码) + web/public/assets/*(构建产物) —— SPA 只调 BFF
  3) BFF 层 : web/server/index.js(对外路由) + web/server/ontology.js
              (向 api_server 转发: apiFetch('...') / fetch(API_URL + '...'))

一条完整的前端链:
    后端路由  <--转发--  ontology.js 函数  <--调用--  index.js 的 BFF 路由
                                                <--fetch--  web/src / 页面层

判定档位
--------
  [页面直连]      页面层(admin.html/food_app)直接引用该后端路径
  [经BFF(SPA)]    ontology.js 转发它, 且承接它的 BFF 路由被 SPA(web/src) 调用
  [仅BFF转发]     ontology.js 转发它, 但没找到 SPA 调用承接的 BFF 路由 —— 半链
  [断链]          web/(排除 node_modules) 全库检索不到任何引用 —— 前端无入口

反向检查
--------
  web/server 转发指向的路径, 在 api_server.py 里是否存在对应路由 —— 缺则报
  「BFF 指向不存在的后端端点」(反向断链)。

路径匹配: 参数段 {kb}/{fname} 与转发里的 ${...} 统一归一化为 `*` 再比对,
避免前缀误判(如 /api/ontology/eval 不匹配 /api/ontology/eval-isolate)。

用法
----
  python scripts/check_chainbreak.py            # 文本报告
  python scripts/check_chainbreak.py --json o.json
退出码: 0 = 无断链; 1 = 存在断链(正向或反向)。
"""
import os
import re
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_SERVER = os.path.join(ROOT, "codes", "api_server.py")
WEB_DIR = os.path.join(ROOT, "web")
SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__"}

ROUTE_RE = re.compile(r'@app\.(get|post|put|delete|patch)\(\s*"([^"]+)"')
BFF_ROUTE_RE = re.compile(r"""url\s*===?\s*['"](/api/[^'"]*)['"]|url\.startsWith\(\s*['"](/api/[^'"]*)['"]""")
ONT_FUNC_RE = re.compile(r'export\s+(?:async\s+)?function\s+(\w+)')
API_TOKEN_RE = re.compile(r'/api/[A-Za-z0-9_/*.\-]*')
PAGE_REFS_RE = re.compile(r'[`\'"](/api/[A-Za-z0-9_/\-]*)')

# 页面层候选(直接引后端): web 顶层的 html + food_app 目录
def _is_page_file(rel):
    rel = rel.replace("\\", "/")
    if rel.endswith(".html"):
        return True
    return "/food_app/" in rel


def norm(path):
    """参数段归一化: {kb}/${...} -> *; 去查询串。"""
    p = re.sub(r'\$\{[^}]*\}', '*', path)
    p = re.sub(r'\{[^}]+\}', '*', p)
    return p.split("?")[0]


def path_match(a, b):
    """归一化路径匹配: '*' 视作通配(可空), 折叠斜杠, 末段前缀兜底。

    保证 /api/ontology/eval 不与 /api/ontology/eval-isolate 互相误匹配。
    """
    def canon(p):
        p = re.sub(r'\*', '', p)
        p = re.sub(r'/+', '/', p).rstrip('/')
        return p or '/'
    a, b = canon(a), canon(b)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def _web_files(exclude_server=False):
    for dp, dn, fn in os.walk(WEB_DIR):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        is_server = "server" in dp.replace("\\", "/").split("/")
        if exclude_server and is_server:
            continue
        for f in fn:
            yield os.path.join(dp, f), is_server


def parse_backend_routes():
    out = set()
    for m in ROUTE_RE.finditer(_read(API_SERVER)):
        out.add((m.group(1).upper(), m.group(2)))
    return sorted(out, key=lambda x: x[1])


def parse_ontology_funcs():
    """web/server/ontology.js: 函数名 -> 它转发的归一化后端路径集合。"""
    funcs, cur = {}, None
    for line in _read(os.path.join(WEB_DIR, "server", "ontology.js")).splitlines():
        m = ONT_FUNC_RE.search(line)
        if m:
            cur = m.group(1)
            funcs.setdefault(cur, set())
        if cur and ("apiFetch(" in line or "API_URL" in line):
            for tm in API_TOKEN_RE.finditer(re.sub(r'\$\{[^}]*\}', '*', line)):
                funcs[cur].add(norm(tm.group(0)))
    return funcs


def parse_bff_routes():
    """web/server/index.js: BFF 对外路由(归一化) -> 处理器内调用函数集合, 保留出现顺序。"""
    cur, routes, order = None, {}, []
    for line in _read(os.path.join(WEB_DIR, "server", "index.js")).splitlines():
        m = BFF_ROUTE_RE.search(line)
        if m:
            cur = norm(m.group(1) or m.group(2))
            if cur not in routes:
                routes[cur] = set()
                order.append(cur)
        if cur:
            for fm in re.finditer(r'\b([a-z][A-Za-z0-9_]{3,})\(', line):
                routes[cur].add(fm.group(1))
    return routes, order


def parse_frontend_paths():
    """页面层 + SPA 层引用的 /api/... 字面量(归一化) -> 文件集合(排除 BFF server 目录)。"""
    refs = {}
    for path, _ in _web_files(exclude_server=True):
        rel = os.path.relpath(path, ROOT)
        if rel.endswith(".map") or rel.endswith((".css", ".svg", ".ico")):
            continue
        txt = _read(path)
        for m in PAGE_REFS_RE.finditer(txt):
            refs.setdefault(norm(m.group(1)), set()).add(rel)
    return refs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="同时写出 JSON 结果")
    args = ap.parse_args()

    if not os.path.exists(API_SERVER):
        print("[!] 未找到 codes/api_server.py，无法审计", file=sys.stderr)
        return 2

    backend = parse_backend_routes()
    ont_funcs = parse_ontology_funcs()
    bff_routes, bff_order = parse_bff_routes()
    fe_paths = parse_frontend_paths()

    bff_targets = {}
    for fn, paths in ont_funcs.items():
        for p in paths:
            bff_targets.setdefault(p, set()).add(fn)

    page_files = [(p, os.path.relpath(p, ROOT)) for p, _ in _web_files(exclude_server=True)
                  if _is_page_file(os.path.relpath(p, ROOT))]

    print("=" * 76)
    print("断链审计报告  check_chainbreak.py")
    print("=" * 76)
    print(f"仓库根            : {ROOT}")
    print(f"后端路由数        : {len(backend)}  (codes/api_server.py)")
    print(f"BFF 对外路由      : {len(bff_order)}  (web/server/index.js)")
    print(f"ontology.js 转发函数 : {len(ont_funcs)}")
    print(f"前端引用路径      : {len(fe_paths)}  (web/, 排除 node_modules)")
    print(f"页面层文件        : {len(page_files)}")
    print()

    rows, broken, half = [], [], []
    for method, path in backend:
        npath = norm(path)

        # ① 页面层直连: 页面文件里出现该路径(参数段按通配匹配)
        if npath == "/":
            page_hits = ["web/index.html"] if os.path.exists(os.path.join(WEB_DIR, "index.html")) else []
        else:
            rx = re.compile(re.escape(npath).replace(r'\*', r'[A-Za-z0-9_./-]+'))
            page_hits = sorted({rel for p, rel in page_files if rx.search(_read(p))})

        # ② ontology.js 转发
        fwd_funcs = sorted({fn for fn, ps in ont_funcs.items()
                            if any(path_match(cp, npath) for cp in ps)})

        # ③ 承接的 BFF 路由, 且被 SPA 调用
        spa_hits, bff_hits = [], []
        for br in bff_order:
            if bff_routes.get(br, set()) & set(fwd_funcs):
                bff_hits.append(br)
                src = sorted({rel for fp, rels in fe_paths.items() if path_match(fp, br)
                              for rel in rels if "web/public" not in rel.replace("\\", "/")})
                if src:
                    spa_hits.append((br, src))

        if page_hits:
            status = "页面直连"
        elif spa_hits:
            status = "经BFF(SPA)"
        elif bff_hits:
            status = "仅BFF转发"
        else:
            status = "断链"

        rows.append((method, path, status, page_hits, fwd_funcs, spa_hits))
        if status == "断链":
            broken.append((method, path))
        elif status == "仅BFF转发":
            half.append((method, path, fwd_funcs))

    ok = [r for r in rows if r[2] in ("页面直连", "经BFF(SPA)")]
    print("-" * 76)
    print(f"[汇总] 有前端入口: {len(ok)}   仅BFF转发(半链): {len(half)}   断链: {len(broken)}")
    print("-" * 76)
    for method, path, status, page_hits, fwd_funcs, spa_hits in rows:
        mark = {"页面直连": "OK", "经BFF(SPA)": "OK", "仅BFF转发": "~~", "断链": "XX"}[status]
        print(f"[{mark}] {method:4s} {path:38s} {status}")
        if page_hits:
            print(f"        证据(页面): {', '.join(page_hits)}")
        if fwd_funcs:
            print(f"        证据(转发): ontology.js:{','.join(fwd_funcs)}")
        if spa_hits:
            br, src = spa_hits[0]
            print(f"        证据(SPA) : BFF {br} <- {', '.join(src)}")

    print()
    print("-" * 76)
    print("[反向] BFF 转发指向的后端路径 是否在 api_server.py 中存在路由")
    print("-" * 76)
    back_norm = {norm(p) for _, p in backend}
    reverse_breaks = []
    for bpath in sorted(bff_targets):
        if not any(path_match(bpath, bp) for bp in back_norm):
            fns = ",".join(sorted(bff_targets[bpath]))
            reverse_breaks.append((bpath, fns))
            print(f"[XX] {bpath:38s} 后端无此路由  (ontology.js:{fns})")
        else:
            print(f"[OK] {bpath}")
    print()
    print(f"[反向汇总] BFF 指向不存在端点: {len(reverse_breaks)}")

    exits = 1 if (broken or reverse_breaks) else 0
    print()
    print("=" * 76)
    if exits == 0:
        print("结论: 未发现断链 (正向 0 / 反向 0)")
    else:
        print(f"结论: 有问题 —— 正向断链 {len(broken)} 项, 反向断链 {len(reverse_breaks)} 项")
    print("=" * 76)

    if args.json:
        data = {
            "backend_routes": [{"method": m, "path": p} for m, p in backend],
            "rows": [{"method": m, "path": p, "status": s, "page_evidence": ph,
                      "forward_funcs": ff, "spa_evidence": [x[0] for x in sb]}
                     for m, p, s, ph, ff, sb in rows],
            "broken": [{"method": m, "path": p} for m, p in broken],
            "half_chain": [{"method": m, "path": p, "funcs": ff} for m, p, ff in half],
            "reverse_breaks": [{"path": p, "funcs": f} for p, f in reverse_breaks],
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"(JSON 已写出: {args.json})")

    return exits


if __name__ == "__main__":
    sys.exit(main())
