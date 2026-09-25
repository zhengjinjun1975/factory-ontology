#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token_guard.py — 令牌吊销 / 刷新轮替 / 限速防爆破 的运行时状态与策略（零依赖，纯标准库 SQLite）。

这是「第 3 轮：令牌吊销/刷新 + 限速防爆破」的状态与策略层。设计纪律：

  · 只加不松：默认不改变任何老行为——老令牌（fotk1/fotk2，无 jti）照常可用；
    本模块只对**显式声明可吊销的新令牌**（fotk3/fotkr1，带 jti）与
    **确实提交了无效凭据的失败尝试**起作用。
  · 状态落到**独立的新目录**（FOOD_AUTH_STATE_DIR，默认 <repo>/var/auth_state），
    绝不在任何 KB 数据目录（codes/data*）下，也不污染 output/。
  · 全部表**有界**：每次写入按过期时间清理（prune）+ 硬上限兜底，绝不允许无界增长。
  · 多进程/多 worker：同一个 SQLite 文件 + busy_timeout + WAL，单机多进程共享同一目录即可；
    跨机/容器部署需把该目录放到共享存储（见报告「不确定项」）。

三块能力：
  ① 令牌吊销（按 jti / 按主体批量）——吊销后立即失效（由 api_server 在鉴权时查 is_revoked）。
  ② 刷新令牌轮替（用后即废）——consume_refresh；已用过的刷新令牌再次使用 → is_consumed 判真，
     api_server 记审计「refresh_reuse」并拒绝。
  ③ 限速/防爆破——按主体与来源 IP 计数，滑窗内失败累计超阈值 → 短时封禁；封禁/解除均可审计。

配置（env，均有默认值；阈值取值依据见 docs/实验/令牌吊销与限速-20260924.md）：
  FOOD_AUTH_STATE_DIR      状态目录，默认 <repo>/var/auth_state
  FOOD_AUTH_FAIL_THRESHOLD 失败阈值，默认 10（滑窗内累计失败数）
  FOOD_AUTH_FAIL_WINDOW    失败计数滑窗，默认 300 秒
  FOOD_AUTH_BAN_SECONDS    封禁时长，默认 300 秒
  FOOD_AUTH_STATE_MAX_ROWS 单表硬上限，默认 50000 行

用法：
    from token_guard import TokenGuard
    g = TokenGuard()
    g.register_token(jti, subject="token-read", role="read", tenant=None, kind="access", exp=...)
    g.is_revoked(jti)            # -> bool
    g.revoke_jti(jti) / g.revoke_subject("token-read")
    g.consume_refresh(jti)       # -> True=首次消费; False=此前已消费(复用)
    g.record_failure("ip:1.2.3.4")   # -> 窗口内计数
    g.ban("ip:1.2.3.4", reason="brute_force")
    g.is_banned("ip:1.2.3.4")    # -> {"banned":bool,"until":float,"reason":str,"just_expired":bool}
    g.stats()                    # 条数/体积/配置
"""
import os
import time
import json
import sqlite3
import hashlib
from datetime import datetime

__all__ = ["TokenGuard", "state_dir", "db_path", "config", "now"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_STATE_DIR = os.path.join(_REPO_ROOT, "var", "auth_state")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    jti      TEXT PRIMARY KEY,
    subject  TEXT,
    role     TEXT,
    tenant   TEXT,
    kind     TEXT NOT NULL,           -- 'access' | 'refresh'
    exp      REAL NOT NULL,
    revoked  INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0,
    ts       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_subject ON tokens(subject);
CREATE INDEX IF NOT EXISTS idx_tokens_exp ON tokens(exp);

CREATE TABLE IF NOT EXISTS auth_fail (
    key  TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fail_key_ts ON auth_fail(key, ts);

CREATE TABLE IF NOT EXISTS bans (
    key    TEXT PRIMARY KEY,
    until  REAL NOT NULL,
    reason TEXT,
    ts     TEXT
);
"""

_BUSY_TIMEOUT_MS = 5000


def now():
    return time.time()


def _env_int(name, default):
    try:
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def state_dir(path=None):
    """状态目录：显式入参 > FOOD_AUTH_STATE_DIR > 默认 <repo>/var/auth_state。"""
    if path:
        return os.path.abspath(path)
    return os.path.abspath(os.environ.get("FOOD_AUTH_STATE_DIR") or _DEFAULT_STATE_DIR)


def db_path(path=None):
    return os.path.join(state_dir(path), "auth_state.db")


def config():
    """限速/封禁参数（可配，默认值有依据）。"""
    return {
        "state_dir": state_dir(),
        "fail_threshold": _env_int("FOOD_AUTH_FAIL_THRESHOLD", 10),
        "fail_window": _env_int("FOOD_AUTH_FAIL_WINDOW", 300),
        "ban_seconds": _env_int("FOOD_AUTH_BAN_SECONDS", 300),
        "max_rows": _env_int("FOOD_AUTH_STATE_MAX_ROWS", 50000),
    }


class TokenGuard(object):
    """令牌吊销 / 刷新轮替 / 限速状态。线程安全：每次操作短连接 + 即时提交。"""

    def __init__(self, path=None):
        self.dir = state_dir(path)
        self.db = db_path(path)
        os.makedirs(self.dir, exist_ok=True)
        self.cfg = config()
        self.cfg["state_dir"] = self.dir
        self._init_schema()

    # ── 基础设施 ──────────────────────────────
    def _conn(self):
        c = sqlite3.connect(self.db, timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout=%d" % _BUSY_TIMEOUT_MS)
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return c

    def _init_schema(self):
        c = self._conn()
        try:
            c.executescript(_SCHEMA)
        finally:
            c.close()

    def _ts(self):
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    # ── 清理（有界）───────────────────────────
    def prune(self, grace_seconds=60):
        """按过期时间清理 + 硬上限兜底。返回各表清理掉的行数。"""
        n0 = now()
        cutoff_tokens = n0 - max(0, int(grace_seconds))
        cutoff_fail = n0 - max(self.cfg["fail_window"], self.cfg["ban_seconds"]) * 2
        removed = {"tokens": 0, "auth_fail": 0, "bans": 0}
        c = self._conn()
        try:
            cur = c.execute("DELETE FROM tokens WHERE exp < ?", (cutoff_tokens,))
            removed["tokens"] = cur.rowcount or 0
            cur = c.execute("DELETE FROM auth_fail WHERE ts < ?", (cutoff_fail,))
            removed["auth_fail"] = cur.rowcount or 0
            cur = c.execute("DELETE FROM bans WHERE until < ?", (n0,))
            removed["bans"] = cur.rowcount or 0
            # 硬上限兜底：仍超则删最旧的，保证文件体积有界
            removed["tokens"] += self._cap(c, "tokens", "exp")
            removed["auth_fail"] += self._cap(c, "auth_fail", "ts")
        finally:
            c.close()
        return removed

    def _cap(self, c, table, order_col):
        mx = int(self.cfg["max_rows"])
        row = c.execute("SELECT COUNT(*) FROM %s" % table).fetchone()
        cnt = row[0] if row else 0
        if cnt <= mx:
            return 0
        over = cnt - mx
        c.execute(
            "DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s ORDER BY %s ASC LIMIT ?)"
            % (table, table, order_col), (over,))
        return over

    # ── ① 令牌登记 / 吊销 ─────────────────────
    def register_token(self, jti, *, subject="", role="", tenant=None, kind="access", exp=None):
        """登记一个可吊销令牌（access/refresh）。exp 必填（epoch 秒）。"""
        if not jti or exp is None:
            return False
        c = self._conn()
        try:
            c.execute(
                "INSERT OR REPLACE INTO tokens (jti, subject, role, tenant, kind, exp, revoked, consumed, ts)"
                " VALUES (?,?,?,?,?,?,0,0,?)",
                (str(jti)[:80], str(subject or "")[:120], str(role or "")[:40],
                 (str(tenant) if tenant else None), str(kind or "access")[:20],
                 float(exp), self._ts()))
        finally:
            c.close()
        return True

    def is_revoked(self, jti):
        """该 jti 是否已被吊销。未知 jti（未登记）→ False（老令牌无 jti 不受影响）。"""
        if not jti:
            return False
        c = self._conn()
        try:
            row = c.execute("SELECT revoked FROM tokens WHERE jti=?", (str(jti),)).fetchone()
        finally:
            c.close()
        return bool(row and row[0])

    def revoke_jti(self, jti, reason=""):
        """按 jti 吊销单个令牌。返回受影响行数（0/1）。已过期被清理的 jti 视为 0。"""
        if not jti:
            return 0
        self.prune()
        c = self._conn()
        try:
            cur = c.execute("UPDATE tokens SET revoked=1 WHERE jti=? AND revoked=0", (str(jti),))
            return cur.rowcount or 0
        finally:
            c.close()

    def revoke_subject(self, subject, reason=""):
        """按主体批量吊销（未过期且未吊销的全部令牌）。返回受影响行数。"""
        if subject is None:
            return 0
        self.prune()
        c = self._conn()
        try:
            cur = c.execute(
                "UPDATE tokens SET revoked=1 WHERE subject=? AND revoked=0 AND exp >= ?",
                (str(subject), now()))
            return cur.rowcount or 0
        finally:
            c.close()

    # ── ② 刷新令牌轮替 ────────────────────────
    def get_token(self, jti):
        if not jti:
            return None
        c = self._conn()
        try:
            row = c.execute(
                "SELECT jti, subject, role, tenant, kind, exp, revoked, consumed FROM tokens WHERE jti=?",
                (str(jti),)).fetchone()
        finally:
            c.close()
        if not row:
            return None
        return {"jti": row[0], "subject": row[1], "role": row[2], "tenant": row[3],
                "kind": row[4], "exp": row[5], "revoked": bool(row[6]), "consumed": bool(row[7])}

    def is_consumed(self, jti):
        """刷新令牌是否已被消费（用后即废 → 复用检测用）。"""
        t = self.get_token(jti)
        return bool(t and t["consumed"])

    def consume_refresh(self, jti):
        """原子消费刷新令牌。返回 True=本次为首次消费（轮替成功）；False=此前已消费（复用，须拒绝）。"""
        if not jti:
            return False
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT consumed FROM tokens WHERE jti=?", (str(jti),)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return False           # 未登记（不是我们签发的刷新令牌）→ 视为不可用
            if row[0]:
                c.execute("COMMIT")
                return False           # 已消费 → 复用
            c.execute("UPDATE tokens SET consumed=1 WHERE jti=?", (str(jti),))
            c.execute("COMMIT")
            return True
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            c.close()

    # ── ③ 限速 / 防爆破 ───────────────────────
    def record_failure(self, key):
        """记一次失败，返回该 key 在滑窗内的累计次数。会顺带做轻量清理。"""
        if not key:
            return 0
        n0 = now()
        c = self._conn()
        try:
            c.execute("INSERT INTO auth_fail (key, ts) VALUES (?,?)", (str(key)[:160], n0))
            cutoff = n0 - self.cfg["fail_window"]
            row = c.execute("SELECT COUNT(*) FROM auth_fail WHERE key=? AND ts>=?",
                            (str(key)[:160], cutoff)).fetchone()
            return row[0] if row else 0
        finally:
            c.close()

    def failure_count(self, key):
        if not key:
            return 0
        cutoff = now() - self.cfg["fail_window"]
        c = self._conn()
        try:
            row = c.execute("SELECT COUNT(*) FROM auth_fail WHERE key=? AND ts>=?",
                            (str(key)[:160], cutoff)).fetchone()
            return row[0] if row else 0
        finally:
            c.close()

    def clear_failures(self, key):
        """鉴权成功 → 清该 key 的失败计数（避免正常用户被前序零星失败累积误伤）。"""
        if not key:
            return
        c = self._conn()
        try:
            c.execute("DELETE FROM auth_fail WHERE key=?", (str(key)[:160],))
        finally:
            c.close()

    def ban(self, key, seconds=None, reason=""):
        """对某 key 施加短时封禁。返回 until（epoch 秒）。"""
        if not key:
            return None
        sec = int(seconds if seconds else self.cfg["ban_seconds"])
        until = now() + max(1, sec)
        c = self._conn()
        try:
            c.execute("INSERT OR REPLACE INTO bans (key, until, reason, ts) VALUES (?,?,?,?)",
                      (str(key)[:160], until, str(reason)[:200], self._ts()))
        finally:
            c.close()
        return until

    def is_banned(self, key):
        """查询封禁状态。自动清理已过期封禁；just_expired=True 表示本次调用刚解除了一个到期封禁（供审计）。"""
        if not key:
            return {"banned": False, "until": None, "reason": "", "just_expired": False}
        c = self._conn()
        try:
            row = c.execute("SELECT until, reason FROM bans WHERE key=?", (str(key),)).fetchone()
            if not row:
                return {"banned": False, "until": None, "reason": "", "just_expired": False}
            until, reason = float(row[0]), (row[1] or "")
            if until <= now():
                c.execute("DELETE FROM bans WHERE key=?", (str(key),))
                return {"banned": False, "until": until, "reason": reason, "just_expired": True}
            return {"banned": True, "until": until, "reason": reason, "just_expired": False}
        finally:
            c.close()

    def unban(self, key):
        """手动解除封禁。返回是否确有封禁被解除。"""
        if not key:
            return False
        c = self._conn()
        try:
            cur = c.execute("DELETE FROM bans WHERE key=?", (str(key),))
            return bool(cur.rowcount)
        finally:
            c.close()

    def active_bans(self):
        self.prune()
        c = self._conn()
        try:
            rows = c.execute("SELECT key, until, reason FROM bans WHERE until>? ORDER BY until",
                             (now(),)).fetchall()
        finally:
            c.close()
        return [{"key": r[0], "until": r[1], "reason": r[2]} for r in rows]

    # ── 观测 ──────────────────────────────────
    def stats(self):
        """状态目录/体积/条数/配置（供 /api/auth/state 与报告实测）。"""
        self.prune()
        c = self._conn()
        try:
            n_tok = c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
            n_rev = c.execute("SELECT COUNT(*) FROM tokens WHERE revoked=1").fetchone()[0]
            n_ref = c.execute("SELECT COUNT(*) FROM tokens WHERE kind='refresh'").fetchone()[0]
            n_cons = c.execute("SELECT COUNT(*) FROM tokens WHERE consumed=1").fetchone()[0]
            n_fail = c.execute("SELECT COUNT(*) FROM auth_fail").fetchone()[0]
            n_ban = c.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
        finally:
            c.close()
        size = 0
        for suf in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.db + suf)
            except Exception:
                pass
        return {
            "state_dir": self.dir, "db_path": self.db, "db_bytes": size,
            "tokens_rows": n_tok, "revoked_rows": n_rev, "refresh_rows": n_ref,
            "consumed_rows": n_cons, "fail_rows": n_fail, "ban_rows": n_ban,
            "config": self.cfg,
        }


def _main():
    import sys
    g = TokenGuard()
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(g.stats(), ensure_ascii=False, indent=2))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "prune":
        print(json.dumps(g.prune(), ensure_ascii=False))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
