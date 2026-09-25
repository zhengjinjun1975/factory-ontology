#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_chain.py — 轻量溯源审计链(零依赖, 纯标准库 SQLite)。

对齐 Semantica 的 verify_chain 思路, 但保持工厂本体的零依赖/轻量纪律:
- 不引图数据库, 不引推理机, 纯 sqlite3 + hashlib。
- 服务卖点: "审核专家 + 企业内训可审计 + 决策可追责"。

能力:
1. 溯源/决策记录 append(record): 每条落 sequence_id + checksum + prev_checksum。
   哈希链三要素(同 Semantica): ①自身 checksum 匹配内容 ②prev_checksum 匹配上一条
   ③sequence_id = 前一条 + 1(无缝隙无重复)。专防"整行硬删除"(删行破坏序号连续性,
   checksum 撞车时 sequence 检查仍能抓缝隙)。
   信任边界: 哈希链防意外/部分损坏 + 非授权局部删改; 不防持有 db 完整重写的攻击者
   (能改 payload 并重算 checksum/prev 则链可整体重建)——审计留痕/可追责用, 非密码学证据.
2. record_decision: 决策当一等公民记录(category/scenario/reasoning/outcome/
   confidence/entities), 带 reasoning 文本哈希, 可被 trace/similar 复用。
3. verify_chain: 全链校验, 返回坏链位置。
4. export_audit: 导出 PROV-O 风格 / JSON / CSV 审计报告(供第三方审计员/合规留档)。

用法:
    from audit_chain import AuditChain
    c = AuditChain("data/audit_chain.db")
    c.record_decision(scenario="阀门选型", reasoning="口径大/对夹安装/价格2560在预算",
                      outcome="选择D371X蝶阀", category="procurement",
                      entities=["Valve_D371X", "RM003"], confidence=0.9)
    ok, bad = c.verify_chain()
    c.export_audit("data/audit_report.json")   # 或 .csv / prov-o

存储: SQLite 单表 ledger(id INTEGER PK AUTOINCREMENT, ts, kind, payload TEXT,
      checksum TEXT, prev_checksum TEXT)。sequence_id = AUTOINCREMENT id,
      天然保证连续递增; 硬删行会在 id 上留缝, verify 可抓。
"""
import os
import json
from datetime import datetime
import sqlite3
import hashlib
import tempfile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- 'trace' | 'decision' | 'note'
    payload       TEXT NOT NULL,            -- JSON 内容
    checksum      TEXT NOT NULL,            -- sha256(payload)
    prev_checksum TEXT                      -- 上一条 checksum (首条为 NULL)
);
"""


def _current_tenant(default=None):
    """当前请求的租户 id（来自 tenant.py 的上下文）；取不到则 default（可指定）。

    审计必须带租户（多租户合规留档）：账本每条记录都写 tenant 字段。
    """
    try:
        import tenant as _t
        cid = _t.current_id()
        if cid:
            return cid
        return default if default is not None else _t.DEFAULT_TENANT
    except Exception:
        return default if default is not None else "default"


class AuditChainError(Exception):
    """链完整性异常。"""


class AuditChain:
    """哈希链溯源账本。线程安全: 每次操作短连接 + 即时提交。

    并发写(2026-09-24 加固): _append 用**单个连接 + BEGIN IMMEDIATE 事务**把
    「读上一条 checksum」与「插入本行」原子化, 并显式设 busy_timeout。
    修复前两者分属两个连接, 8 线程并发写会读到同一 prev_checksum → 链断裂
    (verify_chain 报 chain_break, 实测 200 条里 2 处)。
    """

    # 忙等超时(毫秒): 并发写时等锁而非立刻 "database is locked"
    _BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path=None, tenant=None):
        # 多租户：该账本的兜底租户（未显式传 tenant 且无请求级上下文时用）。
        self.default_tenant = tenant
        # 默认放 temp(不污染仓库); 显式传路径则持久化到指定文件
        self.db_path = db_path or os.path.join(tempfile.gettempdir(), "factory_audit_chain.db")
        # 无已知数据库扩展名时才补 .db(避免 "chain.sqlite3"→"chain.sqlite3.db")
        if os.path.splitext(self.db_path)[1].lower() not in (".db", ".sqlite", ".sqlite3"):
            self.db_path = self.db_path + ".db"
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_schema()

    # ── 基础设施 ──────────────────────────────
    def _conn(self):
        """短连接 + 显式 busy_timeout(默认 5s 忙等, 避免并发下立刻报锁)。"""
        c = sqlite3.connect(self.db_path, timeout=self._BUSY_TIMEOUT_MS / 1000.0)
        try:
            c.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        except Exception:
            pass
        return c

    def _write_conn(self):
        """写专用连接: 自动提交模式(便于自行 BEGIN IMMEDIATE 控制事务边界)。"""
        c = sqlite3.connect(self.db_path, timeout=self._BUSY_TIMEOUT_MS / 1000.0,
                            isolation_level=None)
        try:
            c.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        except Exception:
            pass
        return c

    def _init_schema(self):
        with self._conn() as c:
            c.execute(_SCHEMA)

    @staticmethod
    def _checksum(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _tenant(self, explicit=None):
        """本条记录的租户 id：显式参数 > 请求级上下文 > 账本兜底租户 > 'default'。"""
        if explicit:
            return str(explicit)
        return _current_tenant(default=self.default_tenant)

    def _last_row(self):
        """返回最后一条 (id, checksum) 或 None。"""
        with self._conn() as c:
            cur = c.execute("SELECT id, checksum FROM ledger ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        return row

    def _count(self) -> int:
        with self._conn() as c:
            cur = c.execute("SELECT COUNT(*) FROM ledger")
            return cur.fetchone()[0]

    # ── 记录 ──────────────────────────────────
    def _append(self, kind: str, payload: dict, tenant=None) -> int:
        # 多租户合规留档：每条记录必带 tenant（账本与 JSONL 口径一致）。
        payload.setdefault("tenant", self._tenant(tenant))
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cs = self._checksum(data)
        ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + \
             datetime.now().astimezone().strftime("%z")  # 毫秒+时区(避开 strftime %z/%f 平台坑)
        # 单连接 + BEGIN IMMEDIATE: 读 prev 与插入原子完成, 并发写不会读到同一 prev(防断链)
        c = self._write_conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("SELECT checksum FROM ledger ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            prev_cs = row[0] if row else None
            cur = c.execute(
                "INSERT INTO ledger (ts, kind, payload, checksum, prev_checksum) VALUES (?,?,?,?,?)",
                (ts, kind, data, cs, prev_cs),
            )
            rid = cur.lastrowid
            c.execute("COMMIT")
            return rid
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            c.close()

    def record_trace(self, *, source: str, target: str, relation: str = "uses",
                     detail: str = "") -> int:
        """记录一次溯源查询(原料→批次→产品等)。返回 ledger id。
        kind 内部固定为 trace(与 record_decision/record_note 一致, 不暴露可覆盖)."""
        return self._append("trace", {
            "op": "trace", "source": source, "relation": relation,
            "target": target, "detail": detail[:2000],
        })

    def record_decision(self, *, scenario: str, reasoning: str, outcome: str,
                        category: str = "general", entities=None,
                        confidence: float = 0.5, metadata=None) -> int:
        """记录一条决策(一等公民)。返回 ledger id。

        scenario  决策场景(如"阀门选型")
        reasoning 决策推理(文本, 会被哈希进链)
        outcome   决策结果
        category  分类(procurement/maintenance/... 供 trace/similar 复用)
        entities  关联实体列表(Valve_D371X 等, 供溯源回溯)
        confidence 置信度 0-1
        metadata  附加字段(决策人/时间/依据文件等)
        """
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence 须在 0-1 区间")
        payload = {
            "op": "decision",
            "scenario": scenario,
            "category": category,
            "reasoning": reasoning,
            "reasoning_sha": hashlib.sha256(reasoning.encode("utf-8")).hexdigest()[:16],
            "outcome": outcome,
            "entities": list(entities or []),
            "confidence": float(confidence),
        }
        if metadata:
            payload["metadata"] = metadata
        return self._append("decision", payload)

    def record_note(self, note: str) -> int:
        """记录一条自由备注(审计留痕)。"""
        return self._append("note", {"op": "note", "note": note[:2000]})

    def record_access(self, *, subject: str, action: str, result: str,
                      role: str = "", detail: str = "", metadata=None,
                      tenant=None) -> int:
        """记录一次鉴权/访问事件(商用级加固 2026-09-24)。

        主体(subject) + 动作(action, 如 authenticate/issue_token/whoami) +
        结果(result ∈ grant|deny) + 角色(role)。时间由 _append 统一盖 ts,
        并进入哈希链(防事后删改鉴权记录)。返回 ledger id。

        多租户(2026-09-24): tenant 显式传入则用该值, 否则取请求级租户上下文/账本兜底。
        """
        payload = {
            "op": "access",
            "subject": str(subject or "anonymous")[:200],
            "action": str(action or "access")[:100],
            "result": str(result or "unknown")[:20],
            "role": str(role or "")[:40],
            "detail": str(detail or "")[:500],
        }
        if metadata:
            payload["metadata"] = metadata
        return self._append("access", payload, tenant=tenant)

    # ── 留存 ─────────────────────────────────────
    def retention(self) -> dict:
        """留存概况(合规留档用): 最早/最新 ts, 总条数。

        注: 哈希链不可删行(删行会留序号缝隙并被 verify_chain 抓),
        故链式审计以「全量保留 + 定期 export_audit 归档」为留存策略;
        体量过大的普通访问日志(JSONL)按 ≥6 个月留存期轮转清理(见 api_server)。
        """
        with self._conn() as c:
            cur = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM ledger")
            n, first, last = cur.fetchone()
        return {"total": n or 0, "first_ts": first, "last_ts": last}

    # ── 读取 ──────────────────────────────────
    def get(self, rid: int) -> dict:
        with self._conn() as c:
            cur = c.execute("SELECT id, ts, kind, payload, checksum, prev_checksum FROM ledger WHERE id=?", (rid,))
            row = cur.fetchone()
        if not row:
            raise KeyError(f"ledger 无 id={rid}")
        return {
            "sequence_id": row[0], "ts": row[1], "kind": row[2],
            "payload": json.loads(row[3]), "checksum": row[4], "prev_checksum": row[5],
        }

    def decisions(self, category: str = None, limit: int = 100) -> list:
        """列出决策记录(可过滤 category)。"""
        sql = "SELECT id, ts, kind, payload, checksum, prev_checksum FROM ledger"
        params = []
        if category:
            # payload 是 JSON, 用 LIKE 过滤 category 字段
            sql += " WHERE kind='decision' AND payload LIKE ?"
            params.append(f'%"category": "{category}"%')
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            cur = c.execute(sql, params)
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = json.loads(r[3])
            if category and d.get("category") != category:
                continue  # LIKE 可能误中, 二次精确过滤
            out.append({"sequence_id": r[0], "ts": r[1], "kind": r[2],
                        **d, "checksum": r[4]})
        return out

    # ── 校验(哈希链) ──────────────────────────
    def verify_chain(self) -> tuple:
        """全链校验。返回 (ok: bool, bad: list)。

        校验三要素(对齐 Semantica verify_chain):
         ① 每条自身 checksum == sha256(payload)
         ② 每条 prev_checksum == 上一条的 checksum
         ③ sequence_id 连续无缝隙(id == 前一条+1, 无重复无删行)
        bad 列表元素: (id, 问题类型[checksum_mismatch|chain_break|gap], 详情)
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, payload, checksum, prev_checksum FROM ledger ORDER BY id"
            ).fetchall()
        bad = []
        expected_id = 1
        prev_cs = None
        seen = set()
        for rid, payload, cs, prev_cs_stored in rows:
            # ③ 序号连续(防整行硬删: 删行留缝隙, id != expected_id 即抓)
            if rid != expected_id:
                bad.append((rid, "gap", f"序号缝隙: 期望{expected_id}, 实得{rid}(疑似被删行)"))
                expected_id = rid
            if rid in seen:
                bad.append((rid, "gap", "序号重复(非法)"))
            seen.add(rid)
            expected_id += 1
            # ① 自身校验(防篡改 payload)
            real_cs = self._checksum(payload)
            if real_cs != cs:
                bad.append((rid, "checksum_mismatch", f"payload被篡改: 期望{cs[:12]}.. 实得{real_cs[:12]}.."))
            # ② 链衔接(prev_checksum 必须等于上一条 checksum)
            if prev_cs is not None and prev_cs_stored != prev_cs:
                bad.append((rid, "chain_break", "prev_checksum 与上一条不衔接(链被改动)"))
            prev_cs = cs
        # 首条 prev_checksum 必须为 None
        if rows and rows[0][3] is not None:
            bad.append((rows[0][0], "chain_break", "首条 prev_checksum 应为 NULL"))
        return (len(bad) == 0, bad)

    # ── 导出审计报告 ──────────────────────────
    def export_audit(self, outpath: str, fmt: str = None) -> dict:
        """导出审计报告。fmt 由扩展名推断(json/csv/prov-o)或显式指定。

        返回 {"path":..., "format":..., "n":条数, "verified":ok}
        """
        fmt = fmt or outpath.rsplit(".", 1)[-1].lower()
        # 识别复合扩展: xxx.prov-o.json / xxx.prov-o 都归 prov-o
        low = outpath.lower()
        if fmt == "json" and (".prov-o.json" in low or low.endswith(".prov-o")):
            fmt = "prov-o"
        elif ".prov-o" in low and not low.endswith(".json") and not low.endswith(".csv"):
            fmt = "prov-o"
        ok, bad = self.verify_chain()
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, kind, payload, checksum, prev_checksum FROM ledger ORDER BY id"
            ).fetchall()
        entries = [{
            "sequence_id": r[0], "ts": r[1], "kind": r[2],
            "content": json.loads(r[3]), "checksum": r[4], "prev_checksum": r[5],
        } for r in rows]
        os.makedirs(os.path.dirname(os.path.abspath(outpath)), exist_ok=True)

        if fmt in ("json",):
            doc = {"verified": ok, "integrity_issues": bad, "total": len(entries),
                   "ledger": entries}
            with open(outpath, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            with open(outpath, "w", encoding="utf-8-sig", newline="") as f:
                import csv
                w = csv.writer(f)
                w.writerow(["sequence_id", "ts", "kind", "content", "checksum", "prev_checksum"])
                for r in rows:
                    w.writerow([r[0], r[1], r[2], json.dumps(json.loads(r[3]), ensure_ascii=False),
                                r[4], r[5]])
        elif fmt in ("prov-o", "prov", "prov_owl", "prov-o.json"):
            # PROV-O 风格(轻量): activity/entity 三元组 + 链校验声明
            prov = {
                "@context": {"prov": "http://www.w3.org/ns/prov#"},
                "prefix": {"ex": "http://factory-ontology/audit#"},
                "verified": ok,
                "integrity_issues": bad,
                "activities": [],
                "wasDerivedFrom": [],
            }
            prev_id = None
            for e in entries:
                act_id = f"ex:audit_{e['sequence_id']}"
                prov["activities"].append({
                    "id": act_id,
                    "prov:type": {"prov:value": f"ex:{e['kind']}"},
                    "prov:startedAtTime": e["ts"],
                    "prov:wasAssociatedWith": {"prov:value": "ex:factory-ontology"},
                    "content_hash": e["checksum"],
                })
                if prev_id is not None:
                    prov["wasDerivedFrom"].append({"id": act_id, "prov:used": prev_id})
                prev_id = act_id
            doc = prov
            with open(outpath, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        else:
            raise ValueError(f"不支持的导出格式: {fmt} (支持 json/csv/prov-o)")
        return {"path": outpath, "format": fmt, "n": len(entries), "verified": ok}

    # ── 完整性审计(防删防改) ──────────────────
    def audit_report(self) -> dict:
        """面向审核人员的一句话报告。"""
        total = self._count()
        ok, bad = self.verify_chain()
        n_decision = self._count_kind("decision")
        n_trace = self._count_kind("trace")
        return {
            "total_entries": total, "decision_records": n_decision,
            "trace_records": n_trace, "chain_integrity": "PASS" if ok else "FAIL",
            "integrity_issues": bad,
        }

    def _count_kind(self, kind: str) -> int:
        with self._conn() as c:
            cur = c.execute("SELECT COUNT(*) FROM ledger WHERE kind=?", (kind,))
            return cur.fetchone()[0]


# ── main: CLI 入口 ─────────────────────────────
def main(argv=None):
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__.split("用法:")[1].split("存储:")[0].strip())
        return 0
    cmd = args[0]
    # 允许传 db 路径作为第一参数后的可选项: --db <path>
    db_path = None
    rest = args[1:]
    if rest and rest[0] == "--db":
        db_path = rest[1]
        rest = rest[2:]
    c = AuditChain(db_path)
    if cmd == "record-decision":
        # audit_chain.py record-decision "场景" "推理" "结果" [类别] [--db path]
        if len(rest) < 3:
            print("用法: audit_chain.py record-decision '<场景>' '<推理>' '<结果>' [类别]")
            return 1
        rid = c.record_decision(
            scenario=rest[0], reasoning=rest[1], outcome=rest[2],
            category=rest[3] if len(rest) > 3 else "general",
        )
        print(f"recorded decision id={rid}")
    elif cmd == "verify":
        ok, bad = c.verify_chain()
        print(f"链完整性: {'PASS' if ok else 'FAIL'} ({len(bad)} 处问题)")
        for b in bad[:20]:
            print(f"  [{b[1]}] id={b[0]}: {b[2]}")
        return 0 if ok else 1
    elif cmd == "audit":
        import json as _json
        print(_json.dumps(c.audit_report(), ensure_ascii=False, indent=2))
    elif cmd == "export":
        # audit_chain.py export <out> [--db path]
        if not rest:
            print("用法: audit_chain.py export <out.json|csv|prov-o>")
            return 1
        r = c.export_audit(rest[0])
        print(f"导出 {r['n']} 条 → {r['path']} (verified={r['verified']})")
    else:
        print(f"未知命令: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
