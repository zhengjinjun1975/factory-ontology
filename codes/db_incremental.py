#!/usr/bin/env python3
"""db_incremental.py — 企业 ERP/MES 数据库增量刷新（检测变更 → 只重建模变更表 → 合并更新本体）

在 db_to_ontology.py 一次性全量建本体的基础上，提供**增量刷新**能力：
  1) 检测 DB 表变更（指纹：行数+内容哈希；或用 updated_at 水位列做廉价轮询）
  2) 只对变更的表重跑建模（suggest_schema + to_nt），不重跑未变更表（不触发其 LLM、不重写其三元组）
  3) 把变更表的新三元组**合并**进已有 .nt（按 subject 粒度替换），未变更表的三元组原样保留
  4) 增量合并词典（新增枚举/属性，保留已有同义词映射，增量模式跳过 LLM 聚类）
  5) 状态文件记录每表指纹 + 归属的 subject 列表，供下次变更检测

两种指纹策略（config 里二选一或并存）：
  - content（默认）：md5(全部行内容)+行数。准确，但每次轮询需读全表（适合中小表）。
  - watermark（推荐大表）：config.watermark = {表名: 水位列}。只用 COUNT(*) + MAX(水位列)
    做廉价探测；未变更表不读全量，仅读表头/样本行用于外键推断。

用法：
  python db_incremental.py <config.json>                # 手动单次刷新
  python db_incremental.py <config.json> --watch        # 定时轮询(读 config.poll_interval 秒)
  python db_incremental.py <config.json> --force        # 强制全量重建(重新 LLM 聚类同义词)
  python db_incremental.py <config.json> --status       # 只看当前指纹/变更状态

说明/边界：
  - 首次运行（无状态文件）自动全量建本体建立基线，之后走增量。
  - 增量模式下变更表自身的实例三元组及其"出向"外键引用会被重生成；
    "入向"外键（其它未变更表指向本表）的三元组保留原样。若删除了一个仍被引用的实例，
    可加 --force 全量重建清除悬挂引用。
"""
import os
import sys
import json
import time
import hashlib
import argparse
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
CONF_DIR = os.path.join(ROOT, "config")
STATE = os.path.join(ROOT, "current.json")


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read_config(path):
    """读取并校验增量刷新 config（兼容 db_to_ontology 的 dsn/tables/output + 增量键）。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"[增量] config 文件不存在: {path}")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"[增量] config 不是合法 JSON: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("[增量] config 顶层必须是 JSON 对象")
    if not cfg.get("dsn") and not (cfg.get("db_type") and cfg.get("database")):
        raise ValueError("[增量] config 需提供 dsn(连接串) 或 db_type+database")
    cfg.setdefault("output", "factory_multi")
    cfg.setdefault("poll_interval", 60)
    return cfg


def _state_path(output):
    return os.path.join(CONF_DIR, f"db_state_{output}.json")


def _load_state(output):
    p = _state_path(output)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(output, state):
    json.dump(state, open(_state_path(output), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 指纹

def _content_fp(rows):
    """内容指纹：行数 + md5(全行稳定序列化)。"""
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"c:{len(rows)}:{hashlib.md5(blob).hexdigest()}"


def _conn_kwargs(cfg, db_to_ontology, db_loader):
    """解析 dsn -> 连接 dict（与 db_to_ontology.load_db_tables 一致）。"""
    conn_cfg = dict(cfg)
    if conn_cfg.get("dsn"):
        d = db_loader.parse_dsn(conn_cfg["dsn"])
        conn_cfg.setdefault("db_type", d[0]); conn_cfg.setdefault("host", d[1])
        conn_cfg.setdefault("port", d[2]); conn_cfg.setdefault("user", d[3])
        conn_cfg.setdefault("password", d[4]); conn_cfg.setdefault("database", d[5])
    return conn_cfg


def _watermark_fp(conn_cfg, table, col):
    """水位指纹：SELECT COUNT(*), MAX(col) FROM table → 'w:count:max'。跨 sqlite/mysql/pg。"""
    db_type = (conn_cfg.get("db_type") or "sqlite").lower()
    if db_type in ("postgres", "postgresql", "pg"):
        db_type = "postgres"
    from db_loader import _safe
    table, col = _safe(table, "表名"), _safe(col, "水位列")
    if db_type == "sqlite":
        import sqlite3
        p = conn_cfg.get("database")
        if not os.path.exists(p):
            raise FileNotFoundError(f"SQLite 库不存在: {p}")
        conn = sqlite3.connect(p)
        try:
            cur = conn.execute(f'SELECT COUNT(*), MAX("{col}") FROM "{table}"')
            n, mx = cur.fetchone()
        finally:
            conn.close()
    elif db_type == "mysql":
        import pymysql
        conn = pymysql.connect(host=conn_cfg.get("host"), port=conn_cfg.get("port") or 3306,
                               user=conn_cfg.get("user"), password=conn_cfg.get("password"),
                               database=conn_cfg.get("database"), charset="utf8mb4")
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*), MAX(`{col}`) FROM `{table}`')
        n, mx = cur.fetchone()
        conn.close()
    elif db_type == "postgres":
        import psycopg2
        conn = psycopg2.connect(host=conn_cfg.get("host"), port=conn_cfg.get("port") or 5432,
                                user=conn_cfg.get("user"), password=conn_cfg.get("password"),
                                dbname=conn_cfg.get("database"))
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*), MAX("{col}") FROM "{table}"')
        n, mx = cur.fetchone()
        conn.close()
    else:
        raise ValueError(f"不支持类型: {db_type}")
    return f"w:{n}:{mx}"


def _read_all(cfg, state):
    """读取全部表数据 + 每表指纹。返回 (data, {表: fp})。

    - content 模式：全量读所有表。
    - watermark 模式：只对变更/新表读全量；未变更表仅读表头+1样本行（供外键推断），
      指纹用 COUNT+MAX(水位列) 廉价探测。
    """
    db_to_ontology = _load("db_to_ontology", os.path.join(ROOT, "db_to_ontology.py"))
    db_loader = _load("db_loader", os.path.join(ROOT, "db_loader.py"))
    conn_cfg = _conn_kwargs(cfg, db_to_ontology, db_loader)

    # tables 缺省自动列出
    tables = cfg.get("tables")
    if not tables:
        found = db_to_ontology._list_tables(conn_cfg, db_loader)
        if isinstance(found, dict) and "error" in found:
            raise ValueError(f"[增量] {found['error']}")
        tables = found

    watermark = cfg.get("watermark") or {}
    data, fps = {}, {}
    for t in tables:
        wcol = watermark.get(t)
        conn = dict(conn_cfg); conn["table"] = t
        if wcol:
            fp = _watermark_fp(conn_cfg, t, wcol)
            old = (state.get("tables") or {}).get(t, {}).get("fp")
            if old == fp:
                # 未变更：轻读(表头+1样本行)保持外键推断能力
                light = db_loader.load_db(conn)
                if isinstance(light, dict) and "error" in light:
                    raise ValueError(f"[增量] 读表 {t} 失败: {light['error']}")
                _name, hdrs, _rows = light
                sample = _rows[0] if _rows else {h: "" for h in hdrs}
                data[t] = [sample] if sample else []
                fps[t] = fp
                print(f"  ⏭ 未变更 {t} (watermark 廉价探测)")
                continue
            # 变更：全量读
            res = db_loader.load_db(conn)
            if isinstance(res, dict) and "error" in res:
                raise ValueError(f"[增量] 读表 {t} 失败: {res['error']}")
            _name, _hdrs, rows = res
            data[t] = rows
            fps[t] = fp
            print(f"  ✓ 读表 {t}: {len(rows)} 行 (watermark 变更)")
        else:
            res = db_loader.load_db(conn)
            if isinstance(res, dict) and "error" in res:
                raise ValueError(f"[增量] 读表 {t} 失败: {res['error']}")
            _name, _hdrs, rows = res
            data[t] = rows
            fps[t] = _content_fp(rows)
            print(f"  ✓ 读表 {t}: {len(rows)} 行")
    return data, fps


# ------------------------------------------------------------------ 三元组合并

def _parse_subject_map(lines):
    """三元组行列表 -> {subject: set[(p, o)]}。"""
    import re
    m = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        mm = re.match(r'^<([^>]*)>\s+<([^>]*)>\s+(.+?)\s*\.\s*$', line)
        if not mm:
            continue
        m.setdefault(mm.group(1), set()).add((mm.group(2), mm.group(3)))
    return m


def _read_nt(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return _parse_subject_map(f)
    return {}


def _write_nt(path, subj_map):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for s in sorted(subj_map):
        for p, o in sorted(subj_map[s]):
            lines.append(f"<{s}> <{p}> {o} .")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


def _instance_subjects(subj_map, ns, clsname):
    """取某类名(clsname)实例 subject：前缀 NS+ClassName+'_'。"""
    pref = f"{ns}{clsname}_"
    return [s for s in subj_map if s.startswith(pref)]


def _merge_incremental(data, changed, nt_path, output, state):
    """对变更表重建模并合并进 .nt。返回 (新增行, 删除行, 各变更表subject数)。"""
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    # 1) 仅对变更表建模（触发其 LLM 中文 label，未变更表不建模）
    changed_entities = {}
    for t in changed:
        sc = so.suggest_schema({t: data[t]}) if data.get(t) else {"_entities": {}}
        changed_entities.update(sc["_entities"])
    # 外键推断用全量 data（便宜、无 LLM），保证变更表"出向"外键引用被重生成
    relations = so._infer_relations(data)
    schema = {"_entities": changed_entities, "relations": relations}
    new_lines = so.to_nt({t: data[t] for t in changed if data.get(t)}, schema)
    new_map = _parse_subject_map(new_lines)
    ns = so.NS

    existing = _read_nt(nt_path)
    old_total = sum(len(v) for v in existing.values())

    # 2) 删除变更表旧的实例 subject
    removed = 0
    for t in changed:
        clsname = t.capitalize()
        for s in _instance_subjects(existing, ns, clsname):
            removed += len(existing.pop(s, set()))

    # 3) 加入新 subject（声明行 union 去重；实例行已在上面删旧）
    added = 0
    for s, props in new_map.items():
        if s in existing:
            before = len(existing[s])
            existing[s] |= props
            added += len(existing[s]) - before
        else:
            existing[s] = set(props)
            added += len(props)

    new_total = _write_nt(nt_path, existing)

    # 4) 更新状态：记录变更表当前归属的实例 subject
    for t in changed:
        clsname = t.capitalize()
        state.setdefault("tables", {})[t]["subjects"] = _instance_subjects(new_map, ns, clsname)

    return new_total - old_total, removed, {t: len(_instance_subjects(new_map, ns, t.capitalize())) for t in changed}


# ------------------------------------------------------------------ 词典增量合并（无 LLM，纯枚举收集）

def _collect_lex_for(table_rows, table_columns, existing):
    """从变更表的行收集新增 type/status/zone 枚举 + 属性中文名，合并进已有词典。"""
    import re as _re
    lex = existing
    type_vals = set(); status_vals = set(); zone_vals = set()
    type_cols = [c for c in table_columns if "type" in c.lower() or "类型" in c
                 or c.lower() in ("category", "kind")]
    status_cols = [c for c in table_columns if c.lower() in ("status", "state", "result", "qc_result")]
    zone_cols = [c for c in table_columns if c.lower() in ("region", "location", "zone", "area", "workshop")]
    for row in table_rows:
        for c in type_cols:
            if row.get(c): type_vals.add(str(row[c]).strip())
        for c in status_cols:
            if row.get(c): status_vals.add(str(row[c]).strip())
        for c in zone_cols:
            if row.get(c): zone_vals.add(str(row[c]).strip())
    for v in type_vals - set(lex.get("type_cn2en", {})):
        lex.setdefault("type_cn2en", {})[v] = v
    for v in status_vals - set(lex.get("status_cn2en", {})):
        lex.setdefault("status_cn2en", {})[v] = v
    for v in zone_vals - set(lex.get("zone_cn2en", {})):
        lex.setdefault("zone_cn2en", {})[v] = v
    return len(type_vals | status_vals | zone_vals)


def _merge_lexicon_incremental(cfg, data, changed, output):
    """把变更表的新枚举/属性合并进已有 lexicon_<output>.json。增量模式跳过 LLM 同义词聚类。"""
    lex_path = os.path.join(CONF_DIR, f"lexicon_{output}.json")
    lex = {}
    if os.path.exists(lex_path):
        try:
            lex = json.load(open(lex_path, encoding="utf-8"))
        except Exception:
            lex = {}
    new_terms = 0
    for t in changed:
        rows = data.get(t) or []
        if not rows:
            continue
        new_terms += _collect_lex_for(rows, list(rows[0].keys()), lex)
    # 保证结构完整
    lex.setdefault("attr_cn2en", {}); lex.setdefault("attr_en2cn", {})
    lex.setdefault("status_cn2en", {}); lex.setdefault("type_cn2en", {})
    lex.setdefault("zone_cn2en", {}); lex.setdefault("synonym_map", {})
    lex.setdefault("entity_cn2en", {}); lex.setdefault("numeric_fields", {})
    lex.setdefault("field_aliases", {"status": ["status"],
                                     "deviceType": ["deviceType", "device_type", "type"],
                                     "deviceName": ["deviceName", "device_name", "name"]})
    json.dump(lex, open(lex_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return lex_path, new_terms


# ------------------------------------------------------------------ 主流程

def _init_state(data, output, state, nt_path):
    """首次运行：全量建本体（复用 multi_model.build_data）并记录每表 subject。"""
    mm = _load("multi_model", os.path.join(ROOT, "multi_model.py"))
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    table, tables, n = mm.build_data(data, output)
    subj_map = _read_nt(nt_path)
    ns = so.NS
    state["tables"] = {t: {"fp": None, "subjects": _instance_subjects(subj_map, ns, t.capitalize())}
                       for t in tables}
    state["init"] = True
    return table, tables, n


def incremental_refresh(cfg_path, force=False, status_only=False):
    """执行一次增量刷新。返回描述 dict。"""
    cfg = _read_config(cfg_path)
    output = cfg.get("output")
    nt_path = os.path.join(OUT, f"{output}.nt")
    state = _load_state(output)
    tables_cfg = cfg.get("tables")

    data, fps = _read_all(cfg, state)

    if status_only:
        return {"status": "query", "tables": {t: fps[t] for t in data},
                "state_exists": bool(state.get("init"))}

    # 首次运行（无状态）：全量建基线
    if not state.get("init"):
        table, tables, n = _init_state(data, output, state, nt_path)
        for t in tables:
            state["tables"][t]["fp"] = fps.get(t)
        _save_state(output, state)
        return {"status": "init", "table": table, "tables": tables, "nt_lines": n,
                "note": "首次运行，已全量建本体建立增量基线"}

    # 检测变更
    changed = []
    for t in data:
        old = state.get("tables", {}).get(t, {}).get("fp")
        if old != fps.get(t):
            changed.append(t)

    if not changed and not force:
        return {"status": "no-change", "tables": list(data),
                "note": "未检测到任何表变更，未重建本体"}

    if force:
        changed = list(data)  # 强制全量
        table, tables, n = _init_state(data, output, state, nt_path)
        for t in tables:
            state["tables"][t]["fp"] = fps.get(t)
        _save_state(output, state)
        return {"status": "force-rebuild", "table": table, "tables": tables,
                "nt_lines": n, "note": "强制全量重建（含 LLM 同义词聚类）"}

    # 增量：只改变更表
    delta, removed, per_table = _merge_incremental(data, changed, nt_path, output, state)
    lex_path, new_terms = _merge_lexicon_incremental(cfg, data, changed, output)
    for t in changed:
        state.setdefault("tables", {})[t]["fp"] = fps.get(t)
    _save_state(output, state)

    return {"status": "incremental", "changed": changed, "delta_triples": delta,
            "removed_triples": removed, "per_table_instances": per_table,
            "new_lex_terms": new_terms, "lexicon": os.path.relpath(lex_path, ROOT)}


def _run_watch(cfg_path, force):
    """定时轮询模式。"""
    cfg = _read_config(cfg_path)
    interval = cfg.get("poll_interval") or 60
    print(f"[增量] 定时监控启动: 每 {interval}s 轮询 {cfg_path} (Ctrl+C 退出)")
    try:
        while True:
            try:
                r = incremental_refresh(cfg_path, force=force)
                print(f"[{time.strftime('%H:%M:%S')}] {_fmt(r)}")
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] ❌ 刷新失败: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[增量] 已退出")
    return 0


def _fmt(r):
    if r.get("status") == "incremental":
        return (f"✅ 增量刷新: 变更表 {r['changed']} | 三元组 +{r['delta_triples']}/-{r['removed_triples']} "
                f"| 词典 +{r.get('new_lex_terms')} 词")
    if r.get("status") == "no-change":
        return "⏭ 无变更，跳过"
    if r.get("status") == "init":
        return f"✅ 首次建基线: {len(r['tables'])} 表 -> {r['nt_lines']} 行"
    if r.get("status") == "force-rebuild":
        return f"🔄 强制全量重建: {len(r['tables'])} 表 -> {r['nt_lines']} 行"
    if r.get("status") == "query":
        return f"状态查询: {r['tables']}"
    return str(r)


def main():
    ap = argparse.ArgumentParser(description="ERP 数据库增量刷新（检测变更→只重建模变更表→合并本体）")
    ap.add_argument("config", nargs="?", help="增量 config JSON 路径")
    ap.add_argument("--watch", action="store_true", help="定时轮询模式(读 config.poll_interval)")
    ap.add_argument("--force", action="store_true", help="强制全量重建")
    ap.add_argument("--status", action="store_true", help="只看当前指纹/变更状态，不重建")
    args = ap.parse_args()

    if not args.config:
        print("用法: python db_incremental.py <config.json> [--watch] [--force] [--status]")
        print("     参考 config/db_ontology_incremental.json")
        sys.exit(1)
    try:
        if args.watch:
            sys.exit(_run_watch(args.config, args.force))
        r = incremental_refresh(args.config, force=args.force, status_only=args.status)
        print(_fmt(r))
    except Exception as e:
        print(f"❌ 增量刷新失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
