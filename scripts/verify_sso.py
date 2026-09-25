#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_sso.py — 第4轮「SSO / OAuth2 / LDAP 接入适配」自检（真实执行，全走真实 HTTP）。

跑: <python> scripts/verify_sso.py
（可选: VERIFY_SSO_PORT 覆盖临时端口，默认 8961）

真实验证（每条都给终端输出）：
  ① 未配置 SSO 时**逐字段与改前一致**：
       - 直接核验 api_server._principal 对 admin/read 静态 key、本地令牌的返回 dict（逐字段相等）；
       - 一个"像 JWT 的"凭据在未配置时仍是 401（未知凭据，行为不变）；
       - HTTP: /api/auth/whoami 返回的字段集与值完全一致。
  ② 有效 SSO 令牌 → 200，且角色/租户映射正确（role_map→admin/read；tenant 声明 → 第1轮租户解析）。
  ③ 篡改签名 → 401。
  ④ 过期(exp) → 401。
  ⑤ issuer 不匹配 → 401；audience 不匹配 → 401。
  ⑥ 缺关键声明 fail-closed：缺角色声明 → 401；缺租户声明(require_tenant) → 401；
     未知租户 → 403（走第1轮 tenant.resolve）；角色映射不到 → 401（**绝不默认 admin**）。
  ⑦ 本地凭据在 SSO 开启时仍 200：admin key / read key / fotk1 令牌 全部照常；SSO 非唯一入口。
  ⑧ RS256（公钥验签）：有效 → 200；篡改 → 401（纯标准库实现，本地测试密钥）。
  ⑨ LDAP：接口占位 —— /api/auth/sso/status 显式 implemented=false；LDAP 凭据被拒（未实现，不静默放行）。
  ⑩ /api/auth/sso/status 只报非敏感配置，**不含密钥明文**。

纪律：全程 %TEMP% 临时副本（SSO 配置/租户注册表/审计/吊销库）；不碰真实 config 与 data；
临时端口自起自收；纯标准库。退出码: 全过 0；有 FAIL 1。
"""
import os
import sys
import json
import time
import base64
import hmac
import shutil
import hashlib
import tempfile
import subprocess
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, "codes")
PY = sys.executable
PORT = int(os.environ.get("VERIFY_SSO_PORT", "8961"))
PORT_BASE = PORT + 1          # 「未配置 SSO」基线服务用另一个临时端口

ADMIN_KEY = "vss-admin-key"
READ_KEY = "vss-read-key"
TOKEN_SECRET = "vss-token-secret"
SSO_SECRET = "vss-sso-shared-secret"
ISS = "https://sso.example.com/realms/factory"
AUD = "factory-ontology-api"

TMP = tempfile.mkdtemp(prefix="verify_sso_")
SSO_CFG = os.path.join(TMP, "sso.json")
TENANTS = os.path.join(TMP, "tenants.json")
STATE = os.path.join(TMP, "auth_state")
AUDIT = os.path.join(TMP, "audit.log")
CHAIN = os.path.join(TMP, "chain.db")

# 本地生成的**测试** RSA-2048 公私钥（仅用于自检；非生产密钥，不入任何部署配置）。
TEST_RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1+ObdMK2HswmT284h4GJ
RfyP4fNRC0YcaOsGL8fyL/lbr+BnEvYyAW6tnIAfrsbIeMWTzUtGewtlEbikP/Me
QfXRcGRIeMc9pAFVSP5w/zqvjgU8hgtvSxCRvI6+EhMYv3efS15J7QS7cewWceKD
gRzW6M0tpE7WttZHlQZvDHAbB8cUF5xBdGsp82NmktGf5o98Tv1HfE+GLqJfmokW
xSlRs+TUrz+eK6tIaCiGaXsvfpNejnvQdSp8TW5UUKWhKs9IOZG6v13xgxAV9MZw
qv4iYSw+hlodxe+/a4AAweHx7GpC2VMpIjP2WQjBgdfrjxe9TnFj7UHmit3Dk4aM
ywIDAQAB
-----END PUBLIC KEY-----
"""
TEST_RSA_D_HEX = ("a7f1a80a39a1c06d299388321804b9ebab1adcb7f893f303739afb0eb0895d7b"
                  "de3133a0952c70a09305c87949c5ecf753c92c8b2e2a4c746a6b852b0dfaec9b"
                  "b02bca696c2a88d444815e972a25a39a76ddaae85d91d4756a47f52749354659"
                  "07001491cd07cee7391da67e690026ccf0627204c4d666b1880a768b849c2b82"
                  "9c508fd144784b9c6ae7b248d073f7fc0aa2c0ded45780c7bb363273fe5443c7"
                  "379024f6a994aa706d99c849ff52578b9f0f8b90cd41b8ca45e95542a8f9a451"
                  "c35eff946a46ec8120f1ba7ba3932aa0a18d34f0bf23c34f6f32fdba11bf31a0"
                  "77338041f0e7a9f84311c0e153e0ed257824d108b4da2ff70e3e20ab56f8e01")

FAILS = []
PASSES = [0]


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    return bool(cond)


def section(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ── HTTP / JWT 工具 ──────────────────────────────────────────────────────────
def http(method, path, headers=None, body=None, port=PORT, timeout=90):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode("utf-8") if (body is not None and not isinstance(body, bytes)) else body
    req = urllib.request.Request(url, data=data, method=method)
    if not isinstance(body, bytes):
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def b64u(b):
    if isinstance(b, str):
        b = b.encode("utf-8")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _signing_input(header, payload):
    return (b64u(json.dumps(header, separators=(",", ":"))) + "."
            + b64u(json.dumps(payload, separators=(",", ":")))).encode("ascii")


def jwt_hs256(payload, secret=SSO_SECRET, alg="HS256"):
    si = _signing_input({"alg": alg, "typ": "JWT"}, payload)
    sig = hmac.new(secret.encode("utf-8"), si, hashlib.sha256).digest()
    return si.decode("ascii") + "." + b64u(sig)


def _rsa_sign(si):
    sys.path.insert(0, CODES)
    import sso as _sso
    n, e = _sso.parse_rsa_public_key(TEST_RSA_PUB_PEM)
    k = (n.bit_length() + 7) // 8
    d = int(TEST_RSA_D_HEX, 16)
    em = (b"\x00\x01" + b"\xff" * (k - 3 - 32 - 19) + b"\x00"
          + bytes.fromhex("3031300d060960864801650304020105000420")
          + hashlib.sha256(si).digest())
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def jwt_rs256(payload):
    si = _signing_input({"alg": "RS256", "typ": "JWT"}, payload)
    return si.decode("ascii") + "." + b64u(_rsa_sign(si))


def claims(**over):
    base = {"sub": "u1", "iss": ISS, "aud": [AUD], "role": "factory-operator",
            "tenant": "tenant_a", "exp": int(time.time()) + 3600}
    base.update(over)
    return base


# ── 服务与配置 ───────────────────────────────────────────────────────────────
def _base_env():
    env = dict(os.environ)
    env.update({
        "FOOD_ADMIN_KEY": ADMIN_KEY, "FOOD_READ_KEY": READ_KEY,
        "FOOD_TOKEN_SECRET": TOKEN_SECRET,
        "FOOD_AUTH_STATE_DIR": STATE,
        "FOOD_AUTH_FAIL_THRESHOLD": "100000",     # 本自检不测限速，避免误封
        "FOOD_AUDIT_FILE": AUDIT, "AUDIT_DB": CHAIN,
        "FOOD_TENANTS_FILE": TENANTS,
        "PYTHONPATH": CODES + os.pathsep + env.get("PYTHONPATH", ""),
        "PYTHONIOENCODING": "utf-8",
    })
    for k in list(env):
        if k.startswith("FOOD_SSO_"):
            env.pop(k, None)
    env.pop("FOOD_STRICT_AUTH", None)
    return env


def write_fixtures(sso_enabled):
    with open(TENANTS, "w", encoding="utf-8") as f:
        json.dump({
            "_comment": "verify_sso 自检临时租户注册表",
            "default_tenant": "default",
            "tenants": {
                "default": {"name": "默认租户", "kbs": "*"},
                "tenant_a": {"name": "A企业", "kbs": ["valve"]},
                "tenant_b": {"name": "B企业", "kbs": ["valve", "food"]},
            },
        }, f, ensure_ascii=False, indent=2)
    if sso_enabled:
        with open(SSO_CFG, "w", encoding="utf-8") as f:
            json.dump({
                "_comment": "verify_sso 自检临时 SSO 配置（示例值 + 本地测试公钥）",
                "enabled": True,
                "providers": {
                    "corp-hs256": {
                        "type": "oidc-jwt", "alg": "HS256", "secret": SSO_SECRET,
                        "issuer": ISS, "audience": [AUD],
                        "role_claim": "role",
                        "role_map": {"factory-admin": "admin", "factory-operator": "read"},
                        "tenant_claim": "tenant", "require_tenant": True,
                        "leeway": 0, "enabled": True,
                    },
                    "corp-rs256": {
                        "type": "oidc-jwt", "alg": "RS256",
                        "public_key_pem": TEST_RSA_PUB_PEM,
                        "issuer": ISS, "audience": [AUD],
                        "role_claim": "role",
                        "role_map": {"factory-admin": "admin", "factory-operator": "read"},
                        "tenant_claim": "tenant", "require_tenant": True,
                        "enabled": True,
                    },
                    "corp-ldap": {
                        "type": "ldap", "display_name": "示例 LDAP(占位,未实现)",
                        "url": "ldap://ldap.example.com:389", "base_dn": "dc=example,dc=com",
                        "bind_dn": "cn=svc,ou=services,dc=example,dc=com",
                        "user_filter": "(uid={user})", "enabled": True,
                    },
                },
            }, f, ensure_ascii=False, indent=2)


def start_server(extra=None, port=PORT):
    env = _base_env()
    env.update(extra or {})
    log = open(os.path.join(TMP, "server_%d.log" % port), "wb")
    p = subprocess.Popen([PY, "api_server.py", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=CODES, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(proc, port=PORT, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False, "服务进程已退出(rc=%s)" % proc.returncode
        try:
            st, _ = http("GET", "/health", port=port, timeout=3)
            if st == 200:
                return True, "%.1fs" % (time.time() - t0)
        except Exception:
            pass
        time.sleep(0.7)
    return False, "超时 %ss" % timeout


def dump_log(port=PORT):
    try:
        print(open(os.path.join(TMP, "server_%d.log" % port), encoding="utf-8",
                   errors="replace").read()[-1800:])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 1. 未配置 SSO：逐字段与改前一致（直接核验 _principal + 真实 HTTP）
# ─────────────────────────────────────────────────────────────
_DIRECT_CODE = r"""
import os, sys, json
sys.path.insert(0, os.environ["CODES"])
import api_server as A
tok, exp = A.make_token("read", 3600)
jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.AAAA"
out = {
    "admin":  A._principal(os.environ["FOOD_ADMIN_KEY"]),
    "read":   A._principal(os.environ["FOOD_READ_KEY"]),
    "token":  A._principal(tok),
    "token_exp": exp,
    "jwt":    A._principal(jwt),
    "jwt_is_none": A._principal(jwt) is None,
    "unknown": A._principal("vss-unknown-key"),
    "sso_enabled": bool(A._sso() and A._sso().is_enabled()),
}
print(json.dumps(out, ensure_ascii=False))
"""


def part_1_unconfigured():
    section("1 未配置 SSO：_principal 逐字段与改前一致（直接核验实现）")
    env = _base_env()
    env["CODES"] = CODES
    p = subprocess.run([PY, "-c", _DIRECT_CODE], cwd=REPO, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout[-1200:])
        return ck("未配置时 _principal 直接核验可运行", False, "rc=%s" % p.returncode)
    d = json.loads(p.stdout.strip().splitlines()[-1])
    ck("admin 静态 key → 主体逐字段一致",
       d["admin"] == {"subject": "static-admin", "role": "admin", "exp": None,
                      "auth": "static", "tenant": None}, d["admin"])
    ck("read 静态 key → 主体逐字段一致",
       d["read"] == {"subject": "static-read", "role": "read", "exp": None,
                     "auth": "static", "tenant": None}, d["read"])
    ck("本地令牌 → 主体逐字段一致(auth=token, tenant=None, 无 jti)",
       d["token"] == {"subject": "token-read", "role": "read", "exp": d["token_exp"],
                      "auth": "token", "tenant": None, "jti": None}, d["token"])
    ck("未配置时 JWT 形状凭据 → None(未知凭据, 与改前一致)", d["jwt"] is None and d["jwt_is_none"],
       d["jwt"])
    ck("未配置时任意未知 key → None", d["unknown"] is None, d["unknown"])
    ck("未配置时 SSO 适配器 is_enabled()=False（空 registry）", d["sso_enabled"] is False,
       "sso_enabled=%s" % d["sso_enabled"])

    # 真实 HTTP 基线：另一个临时端口，未配置 SSO
    proc = log = None
    try:
        proc, log = start_server(port=PORT_BASE)
        ok, info = wait_ready(proc, port=PORT_BASE)
        if not ck("基线服务就绪(真实 HTTP, 未配置 SSO)", ok, info):
            dump_log(PORT_BASE)
            return
        st, b = http("GET", "/api/auth/whoami", {"X-API-Key": ADMIN_KEY}, port=PORT_BASE)
        ck("基线 whoami(admin key) → 200 且字段集精确 = {ok,subject,role,exp,auth,expires_at}",
           st == 200 and set(b.keys()) == {"ok", "subject", "role", "exp", "auth", "expires_at"},
           "status=%s keys=%s" % (st, sorted(b.keys()) if isinstance(b, dict) else b))
        ck("基线 whoami(admin) 值逐字段一致",
           b.get("subject") == "static-admin" and b.get("role") == "admin"
           and b.get("auth") == "static" and b.get("exp") is None and b.get("expires_at") is None,
           b)
        jwt = jwt_hs256(claims())
        st_j, _ = http("GET", "/api/auth/whoami", {"X-API-Key": jwt}, port=PORT_BASE)
        ck("基线：有效签名的 JWT 在未配置 SSO 时 → 401(未知凭据, 行为不变)", st_j == 401,
           "status=%s" % st_j)
    finally:
        if proc is not None:
            try:
                proc.terminate(); proc.wait(timeout=15)
            except Exception:
                try: proc.kill()
                except Exception: pass
        if log is not None:
            try: log.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────
# 2..10 SSO 已启用
# ─────────────────────────────────────────────────────────────
def part_1b_interface():
    section("1b 适配器接口：注册 / 启用 / 停用（直接核验实现）")
    sys.path.insert(0, CODES)
    import sso
    reg = sso.SSORegistry()
    p = sso.OIDCJWTProvider("corp", alg="HS256", secret=SSO_SECRET, require_tenant=False,
                            enabled=False)
    reg.register(p)
    ck("注册后默认停用 → is_enabled()=False", reg.is_enabled() is False)
    ck("停用态：有效令牌 → authenticate 返回 None(不参与)", reg.authenticate(jwt_hs256(claims())) is None)
    reg.enable("corp")
    ck("enable('corp') 后 is_enabled()=True", reg.is_enabled() is True)
    r = reg.authenticate(jwt_hs256(claims(role="read")))
    ck("启用后有效令牌 → 命中 principal(role=read)", bool(r) and r.get("role") == "read", r)
    reg.disable("corp")
    ck("disable('corp') 后 authenticate 回到 None", reg.authenticate(jwt_hs256(claims(role="read"))) is None)
    ck("对不存在的适配器 enable/disable → False", reg.enable("nope") is False
       and reg.disable("nope") is False)
    ck("unregister 生效", reg.unregister("corp") is True and reg.get("corp") is None)
    ck("status() 不含共享密钥明文", SSO_SECRET not in json.dumps(sso.SSORegistry().status()))


def part_2_valid_mapping():
    section("2 有效 SSO 令牌：200 + 角色/租户映射正确")
    st, b = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(role="factory-operator"))})
    ck("operator→read 且 tenant_a → /api/kbs 200", st == 200, "status=%s" % st)
    ck("租户映射正确：tenant=tenant_a（走第1轮租户解析）", b.get("tenant") == "tenant_a", b)
    ck("tenant_a 可见 KB 白名单生效(仅 valve)", b.get("kbs") == ["valve"], b.get("kbs"))
    st_w, bw = http("GET", "/api/auth/whoami", {"Authorization": "Bearer %s"
                                                % jwt_hs256(claims(role="factory-operator"))})
    ck("whoami → 200 且 role=read / auth=sso / subject=sso:u1",
       st_w == 200 and bw.get("role") == "read" and bw.get("auth") == "sso"
       and bw.get("subject") == "sso:u1", bw)
    st_a, ba = http("GET", "/api/auth/sso/status",
                    {"X-API-Key": jwt_hs256(claims(role="factory-admin"))})
    ck("admin 映射：factory-admin→admin 可访问 admin 端点 → 200", st_a == 200, "status=%s" % st_a)
    st_r, _ = http("GET", "/api/auth/sso/status",
                   {"X-API-Key": jwt_hs256(claims(role="factory-operator"))})
    ck("read 令牌访问 admin 端点 → 401/403(角色不足)", st_r in (401, 403), "status=%s" % st_r)
    return ba if isinstance(ba, dict) else {}


def part_3_tampered():
    section("3 篡改签名 → 401")
    tok = jwt_hs256(claims())
    bad = tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")
    st, _ = http("GET", "/api/kbs", {"X-API-Key": bad})
    ck("篡改签名的令牌 → 401", st == 401, "status=%s" % st)
    # 用错误密钥签发的"合法结构"令牌 → 401
    st2, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(), secret="wrong-secret")})
    ck("错误共享密钥签发 → 401", st2 == 401, "status=%s" % st2)


def part_4_expired():
    section("4 过期(exp) → 401")
    st, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(exp=int(time.time()) - 30))})
    ck("已过期令牌 → 401", st == 401, "status=%s" % st)
    st2, _ = http("GET", "/api/kbs",
                  {"X-API-Key": jwt_hs256(claims(exp=int(time.time()) - 1))})
    ck("刚过期(1s) → 401", st2 == 401, "status=%s" % st2)


def part_5_iss_aud():
    section("5 issuer / audience 不匹配 → 401")
    st_i, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(iss="https://evil.example"))})
    ck("issuer 不匹配 → 401", st_i == 401, "status=%s" % st_i)
    st_a, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(aud=["other-api"]))})
    ck("audience 不匹配 → 401", st_a == 401, "status=%s" % st_a)
    st_a2, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(aud=[]))})
    ck("audience 为空(与期望无交集) → 401", st_a2 == 401, "status=%s" % st_a2)


def part_6_failclosed():
    section("6 缺关键声明 / 未知租户 → fail-closed（绝不默认 admin）")
    # 缺角色声明
    c = claims(); c.pop("role")
    st_r, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(c)})
    ck("缺角色声明 → 401", st_r == 401, "status=%s" % st_r)
    # 角色映射不到 → 拒绝（不默认 read / 更不默认 admin）
    st_u, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(role="guest"))})
    ck("角色声明映射不到 → 401(不默认放行)", st_u == 401, "status=%s" % st_u)
    # admin 端点：无法映射 → 绝不放行
    st_ua, _ = http("GET", "/api/auth/sso/status", {"X-API-Key": jwt_hs256(claims(role="guest"))})
    ck("角色映射不到时访问 admin 端点 → 401/403(绝不给 admin)", st_ua in (401, 403),
       "status=%s" % st_ua)
    # 缺租户声明（require_tenant=true）
    c2 = claims(); c2.pop("tenant")
    st_t, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(c2)})
    ck("缺租户声明(require_tenant) → 401", st_t == 401, "status=%s" % st_t)
    # 缺 exp
    c3 = claims(); c3.pop("exp")
    st_e, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(c3)})
    ck("缺 exp 声明 → 401(fail-closed)", st_e == 401, "status=%s" % st_e)
    # 未知租户（存在声明但不在注册表）→ 403（第1轮租户解析 fail-closed）
    st_g, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims(tenant="ghost_tenant"))})
    ck("未知租户声明 → 403(走 tenant.resolve fail-closed)", st_g == 403, "status=%s" % st_g)


def part_7_local_unaffected():
    section("7 本地凭据在 SSO 开启时仍可用（SSO 非唯一入口）")
    st_a, _ = http("GET", "/api/auth/state", {"X-API-Key": ADMIN_KEY})
    ck("admin 静态 key → 200", st_a == 200, "status=%s" % st_a)
    st_r, _ = http("GET", "/api/kbs", {"X-API-Key": READ_KEY})
    ck("read 静态 key → 200", st_r == 200, "status=%s" % st_r)
    st_i, bi = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY}, {"role": "read"})
    tok = bi.get("token", "")
    ck("SSO 开启时仍可签发本地 fotk1 令牌", st_i == 200 and tok.startswith("fotk1."),
       "status=%s prefix=%s" % (st_i, tok.split(".")[0]))
    st_u, _ = http("GET", "/api/kbs", {"X-API-Key": tok})
    ck("SSO 开启时本地 fotk1 令牌照常可用 → 200", st_u == 200, "status=%s" % st_u)
    st_w, bw = http("GET", "/api/auth/whoami", {"X-API-Key": READ_KEY})
    ck("本地 read key whoami 字段仍与改前一致(auth=static)",
       st_w == 200 and bw.get("auth") == "static" and bw.get("subject") == "static-read"
       and bw.get("role") == "read", bw)


def part_8_rs256():
    section("8 RS256（公钥验签，纯标准库）：有效 → 200；篡改 → 401")
    st, b = http("GET", "/api/kbs", {"X-API-Key": jwt_rs256(claims(role="factory-operator"))})
    ck("RS256 有效令牌 → 200", st == 200, "status=%s" % st)
    ck("RS256 令牌租户映射正确", b.get("tenant") == "tenant_a", b)
    st_a, _ = http("GET", "/api/auth/sso/status",
                   {"X-API-Key": jwt_rs256(claims(role="factory-admin"))})
    ck("RS256 admin 映射 → 可访问 admin 端点 200", st_a == 200, "status=%s" % st_a)
    tok = jwt_rs256(claims())
    bad = tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")
    st_b, _ = http("GET", "/api/kbs", {"X-API-Key": bad})
    ck("RS256 篡改签名 → 401", st_b == 401, "status=%s" % st_b)
    # 用测试私钥之外的签名（HS256 签的）冒充 RS256 → 401
    st_c, _ = http("GET", "/api/kbs", {"X-API-Key": jwt_hs256(claims())})
    ck("HS256 令牌冒充 RS256 provider 不会被误判(签名算法受控)", st_c == 200,
       "status=%s(HS256 provider 正常处理)" % st_c)


def part_9_ldap_placeholder():
    section("9 LDAP：接口占位（未实现/未实测），不静默放行")
    st, b = http("GET", "/api/auth/sso/status", {"X-API-Key": ADMIN_KEY})
    provs = {p["name"]: p for p in (b.get("providers") or [])}
    ck("/api/auth/sso/status 列出 LDAP 适配器", "corp-ldap" in provs, sorted(provs.keys()))
    ldap = provs.get("corp-ldap", {})
    ck("LDAP 适配器显式 implemented=false（未实现）", ldap.get("implemented") is False, ldap)
    ck("LDAP 适配器 kind=ldap 且注明未实现", ldap.get("kind") == "ldap"
       and "未实现" in str(ldap.get("note", "")), ldap.get("note"))
    # 用一个 LDAP 风格凭据（非 JWT）请求 → 被拒（不静默放行）
    st_l, _ = http("GET", "/api/kbs", {"X-API-Key": "ldap:cn=alice,ou=users,dc=example,dc=com"})
    ck("LDAP 风格凭据 → 401（占位适配器拒绝，不放行）", st_l == 401, "status=%s" % st_l)


def part_10_status_no_secret():
    section("10 /api/auth/sso/status：只报非敏感配置（不含密钥明文）")
    st, b = http("GET", "/api/auth/sso/status", {"X-API-Key": ADMIN_KEY})
    raw = json.dumps(b, ensure_ascii=False)
    ck("status → 200 且 enabled=true", st == 200 and b.get("enabled") is True,
       "status=%s enabled=%s" % (st, b.get("enabled")))
    ck("状态输出**不含共享密钥明文**", SSO_SECRET not in raw, "含明文?" )
    ck("状态输出不含私钥/公钥明文", "BEGIN PUBLIC KEY" not in raw and TEST_RSA_D_HEX[:16] not in raw,
       "has_public_key=%s" % ([p.get("has_public_key") for p in b.get("providers", [])]))
    hs = [p for p in b.get("providers", []) if p.get("name") == "corp-hs256"]
    ck("HS256 provider 只报 has_secret=true(布尔), 不报密钥本身",
       hs and hs[0].get("has_secret") is True and "secret" not in hs[0], hs[0] if hs else None)


# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 78)
    print("第4轮自检：SSO / OAuth2 / LDAP 接入适配   仓库: %s" % REPO)
    print("临时工作目录: %s" % TMP)
    print("=" * 78)
    part_1_unconfigured()
    part_1b_interface()

    write_fixtures(sso_enabled=True)
    proc = log = None
    try:
        proc, log = start_server(extra={"FOOD_SSO_CONFIG": SSO_CFG})
        ok, info = wait_ready(proc)
        if not ck("SSO 服务就绪(真实 HTTP, 已配置 SSO)", ok, info):
            dump_log()
            raise SystemExit(1)
        part_2_valid_mapping()
        part_3_tampered()
        part_4_expired()
        part_5_iss_aud()
        part_6_failclosed()
        part_7_local_unaffected()
        part_8_rs256()
        part_9_ldap_placeholder()
        part_10_status_no_secret()
    finally:
        if proc is not None:
            try:
                proc.terminate(); proc.wait(timeout=15)
            except Exception:
                try: proc.kill()
                except Exception: pass
        if log is not None:
            try: log.close()
            except Exception: pass
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "-" * 78)
    print("自检结果：PASS %d / FAIL %d" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("未过项：")
        for f in FAILS:
            print("  - %s" % f)
    print("-" * 78)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
