#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sso.py — 可选 SSO / OIDC(JWT) / LDAP 接入适配器（纯标准库，零第三方依赖）。

这是「第 4 轮：SSO / OAuth2 / LDAP 接入适配」的适配层。设计纪律与前三轮一致：

  · 只加不松：默认**未启用/未配置** → api_server._principal 行为逐字段不变。
    本地静态 key 与本地令牌(fotk*)路径**永远优先**；SSO 只作「附加」凭据入口。
    SSO **绝不是唯一入口** —— 离线私有化部署必须仍能只用本地凭据。
  · fail-closed：令牌必须 签名有效 + 未过期(+nbf) + issuer/audience 匹配 + 关键声明齐全，
    任一不满足一律拒绝；角色映射不到 / 缺关键声明一律拒绝，**绝不默认给管理员**。
  · 零第三方依赖：HS256 用 hmac/hashlib；RS256 用纯标准库 RSA(PKCS#1 v1.5)+最小 DER 解析。
  · 不联网：共享密钥 / 公钥由本地配置（或环境变量）提供；不外呼 JWKS。多进程/密钥轮换注意点见报告。

适配器接口（可注册 / 启用 / 停用）：
    reg = SSORegistry()
    reg.register(OIDCJWTProvider("corp", alg="HS256", secret="...", issuer="...", audience=[...]))
    reg.enable("corp") / reg.disable("corp") / reg.get("corp") / reg.enabled_providers()
    reg.authenticate(token)   # 按已启用适配器顺序尝试；命中返回 principal，否则 None
    reg.is_enabled() / reg.status()   # 观测（**不含任何密钥明文**）

从环境 / 配置装载：
    from_env() → SSORegistry。读 FOOD_SSO_CONFIG（默认 codes/config/sso.json）。
    文件缺失 或 enabled=false → 空 registry（authenticate 恒 None，即「未配置 SSO」）。
    环境变量覆盖（便于测试/单机部署，不进配置文件）：
      FOOD_SSO_CONFIG            配置文件路径（默认 codes/config/sso.json）
      FOOD_SSO_ENABLED           1/true/yes/on 强制启用全部；0/false 强制停用全部
      FOOD_SSO_HS256_SECRET      合成一个 HS256 provider 的共享密钥（配置为空时用）
      FOOD_SSO_PROVIDER          环境覆盖作用到哪个 provider（默认第一个）
      FOOD_SSO_ISSUER / FOOD_SSO_AUDIENCE / FOOD_SSO_ROLE_CLAIM /
      FOOD_SSO_TENANT_CLAIM / FOOD_SSO_REQUIRE_TENANT / FOOD_SSO_LEEWAY

principal 结构（与 api_server._principal 完全兼容）：
    {"subject","role","exp","auth":"sso","tenant","iss"}
  或 {"denied_reason": "..."}（已识别但被拒 → api_server 记 401）
  或 None（非本适配器的凭据 → 维持「未知凭据」语义，行为与未接 SSO 时一致）

LDAP：**只做接口占位**（标准库无 LDAP 客户端，不为此引第三方依赖）。
    LDAPProvider.authenticate() 恒返回未实现原因，绝不静默放行。接入方式见模块末与报告。
"""
import os
import json
import time
import hmac
import base64
import hashlib

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config", "sso.json")
ENV_CONFIG = "FOOD_SSO_CONFIG"

_ROLE_ADMIN = "admin"
_ROLE_READ = "read"
_VALID_ROLES = (_ROLE_ADMIN, _ROLE_READ)

__all__ = [
    "SSOError", "SSORejected",
    "Provider", "OIDCJWTProvider", "LDAPProvider", "SSORegistry",
    "config_path", "load_config", "from_env",
]


class SSOError(Exception):
    """SSO 相关错误基类。"""


class SSORejected(SSOError):
    """令牌已识别但被拒（携带 HTTP 语义的状态码，默认 401）。"""

    def __init__(self, reason, status=401):
        self.reason = str(reason)
        self.status = int(status)
        super().__init__(self.reason)


# ── JWT 基础（base64url / 分段解析）───────────────────────────────────────────
def _b64url_decode(s):
    """base64url 解码（容忍缺失的 = 填充）。"""
    if isinstance(s, str):
        s = s.encode("ascii")
    pad = b"=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64url_encode(b):
    """base64url 编码（无 = 填充）。供测试/工具用。"""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _jwt_parts(token):
    """拆解紧凑 JWT → (header, payload, signing_input_bytes, sig_bytes)。

    仅当**形状像 JWT** 时返回；否则返回 None（→ 调用方视为「不是本适配器的凭据」，
    维持 api_server 原有「未知凭据」语义，绝不因接 SSO 而改变本地凭据行为）。
    """
    if not isinstance(token, str):
        return None
    s = token.strip()
    if s.count(".") != 2:
        return None
    a, b, c = s.split(".")
    if not a or not b:
        return None
    try:
        header = json.loads(_b64url_decode(a))
        payload = json.loads(_b64url_decode(b))
        sig = _b64url_decode(c)
    except Exception:
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    if not header.get("alg"):
        return None
    return header, payload, (a + "." + b).encode("ascii"), sig


def _get_claim(payload, path):
    """取声明；路径支持点号（如 realm_access.roles）。缺失 → None。"""
    if not path:
        return None
    cur = payload
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ── 纯标准库 RS256 验签（RSA PKCS#1 v1.5 + SHA-256）────────────────────────────
# DigestInfo 前缀：SEQUENCE( AlgorithmIdentifier(sha256, NULL), OCTET STRING(0x20) )
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der_read(data, i):
    """读一个 DER TLV → (tag, value_bytes, next_index)。"""
    tag = data[i]
    i += 1
    ln = data[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[i:i + n], "big")
        i += n
    return tag, data[i:i + ln], i + ln


def _pem_to_der(pem):
    """PEM → DER（只认 base64 行，忽略 ----- 头尾）。"""
    lines = [ln.strip() for ln in str(pem).strip().splitlines()
             if ln.strip() and "-----" not in ln]
    return base64.b64decode("".join(lines))


def parse_rsa_public_key(pem):
    """SubjectPublicKeyInfo PEM → (n, e)。纯标准库最小 DER 解析。

    支持 openssl 生成的 '-----BEGIN PUBLIC KEY-----'（SPKI）格式。
    """
    der = _pem_to_der(pem)
    _tag, spki, _ = _der_read(der, 0)              # SEQUENCE { AlgId, BIT STRING }
    _t, _algid, j = _der_read(spki, 0)             # AlgorithmIdentifier
    _t, bitstr, _ = _der_read(spki, j)             # BIT STRING
    if bitstr[:1] == b"\x00":                      # 去掉未使用位计数
        bitstr = bitstr[1:]
    _t, rsa, _ = _der_read(bitstr, 0)              # RSAPublicKey { n, e }
    _t, nbytes, k = _der_read(rsa, 0)
    _t, ebytes, _ = _der_read(rsa, k)
    n = int.from_bytes(nbytes, "big")
    e = int.from_bytes(ebytes, "big")
    if n <= 0 or e <= 0:
        raise SSOError("公钥解析失败：n/e 非正")
    return n, e


def verify_rs256(public_key_pem, signing_input, signature):
    """RS256 验签：sig = (PKCS#1 v1.5(SHA256(msg)))^d mod n 的逆运算。

    真：pow(sig, e, n) 还原出 00 01 FF..FF 00 || DigestInfo(SHA-256) 且摘要一致。
    常量时间比较摘要；任何结构不符 → False（拒绝）。**
    """
    n, e = parse_rsa_public_key(public_key_pem)
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    if em[0] != 0x00 or em[1] != 0x01:
        return False
    idx = em.find(b"\x00", 2)
    if idx < 10:                                   # 至少 8 字节 0xFF 填充
        return False
    if any(b != 0xFF for b in em[2:idx]):
        return False
    digestinfo = em[idx + 1:]
    expect = _SHA256_DIGESTINFO_PREFIX + hashlib.sha256(signing_input).digest()
    return hmac.compare_digest(digestinfo, expect)


def verify_hs256(secret, signing_input, signature):
    """HS256 验签（共享密钥，常量时间比较）。"""
    if not secret:
        return False
    expect = hmac.new(str(secret).encode("utf-8"), signing_input, hashlib.sha256).digest()
    return hmac.compare_digest(expect, signature)


# ── 适配器基类 ───────────────────────────────────────────────────────────────
class Provider(object):
    """SSO 适配器基类。子类实现 authenticate()。"""

    kind = "generic"

    def __init__(self, name, enabled=False, display_name=""):
        self.name = str(name)
        self.enabled = bool(enabled)
        self.display_name = str(display_name or name)

    def authenticate(self, token):        # pragma: no cover - 抽象
        raise NotImplementedError

    def status(self):
        """观测用（**不含密钥**）。"""
        return {"name": self.name, "kind": self.kind,
                "enabled": bool(self.enabled), "display_name": self.display_name}


class OIDCJWTProvider(Provider):
    """OIDC / JWT 适配器：HS256(共享密钥) 或 RS256(公钥) 验签 + 声明映射。

    参数（全部可配）：
      alg             'HS256'（默认）或 'RS256'；其它一律拒绝（含 'none'）。
      secret          HS256 共享密钥（本地示例值，勿写真实生产密钥进仓库）。
      public_key_pem  RS256 公钥 PEM（SPKI）。
      issuer          Issuer；非空 → 令牌 iss 必须精确匹配，否则拒绝。
      audience        Audience（str / list）；非空 → 令牌 aud 必须与之有交集，否则拒绝。
      role_claim      角色声明路径（默认 'role'；如 Keycloak 用 'realm_access.roles'）。
      role_map        {外部角色值 → admin|read}；缺省时只认字面 'admin'/'read'。
      tenant_claim    租户声明路径（默认 'tenant'）。
      require_tenant  True → 缺租户声明直接拒绝；False → 允许无租户(落默认租户)。
      leeway          时钟偏移容忍秒（默认 0）。
      clock           取当前时间（测试可注入）。
    """

    kind = "oidc-jwt"

    def __init__(self, name, *, alg="HS256", secret="", public_key_pem="",
                 issuer="", audience=None, role_claim="role", role_map=None,
                 tenant_claim="tenant", require_tenant=False, leeway=0,
                 enabled=False, display_name="", clock=None):
        super().__init__(name, enabled=enabled, display_name=display_name)
        self.alg = str(alg or "HS256").upper()
        self.secret = str(secret or "")
        self.public_key_pem = str(public_key_pem or "")
        self.issuer = str(issuer or "")
        if isinstance(audience, str):
            self.audience = [audience] if audience else []
        else:
            self.audience = [str(a) for a in (audience or [])]
        self.role_claim = str(role_claim or "role")
        self.role_map = dict(role_map) if isinstance(role_map, dict) else None
        self.tenant_claim = str(tenant_claim or "tenant")
        self.require_tenant = bool(require_tenant)
        self.leeway = int(leeway or 0)
        self._clock = clock or time.time

    def status(self):
        st = super().status()
        st.update({"alg": self.alg, "issuer": self.issuer, "audience": list(self.audience),
                   "role_claim": self.role_claim,
                   "role_map_keys": (sorted(self.role_map.keys()) if self.role_map else None),
                   "tenant_claim": self.tenant_claim, "require_tenant": self.require_tenant,
                   "has_secret": bool(self.secret), "has_public_key": bool(self.public_key_pem)})
        return st

    def _map_role(self, payload):
        """角色声明 → admin/read；映射不到 → None（fail-closed，绝不默认 admin）。"""
        raw = _get_claim(payload, self.role_claim)
        if raw is None or raw == "":
            return None
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        out = None
        for v in values:
            if isinstance(v, str):
                cand = self.role_map.get(v) if self.role_map else (v if v in _VALID_ROLES else None)
            else:
                cand = None
            if cand not in _VALID_ROLES:
                continue
            if cand == _ROLE_ADMIN:
                return _ROLE_ADMIN            # admin 具有最高优先级
            out = _ROLE_READ
        return out

    def authenticate(self, token):
        if not self.enabled:
            return None
        parts = _jwt_parts(token)
        if parts is None:
            return None                        # 非 JWT 形状 → 不是本适配器的凭据
        header, payload, signing_input, sig = parts

        alg = str(header.get("alg", "")).upper()
        if alg in ("", "NONE") or alg != self.alg:
            return {"denied_reason": "SSO 算法不受支持: %r（期望 %s）" % (alg, self.alg)}

        if self.alg == "HS256":
            if not self.secret:
                return {"denied_reason": "SSO(HS256) 未配置共享密钥"}
            ok = verify_hs256(self.secret, signing_input, sig)
        elif self.alg == "RS256":
            if not self.public_key_pem:
                return {"denied_reason": "SSO(RS256) 未配置公钥"}
            try:
                ok = verify_rs256(self.public_key_pem, signing_input, sig)
            except Exception:
                ok = False
        else:
            return {"denied_reason": "SSO 算法不受支持: %r" % alg}
        if not ok:
            return {"denied_reason": "SSO 令牌签名校验失败(或被篡改)"}

        now = self._clock()
        exp = payload.get("exp")
        if exp is None:
            return {"denied_reason": "SSO 令牌缺少 exp 声明(fail-closed)"}
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            return {"denied_reason": "SSO exp 声明非法"}
        if now > exp + self.leeway:
            return {"denied_reason": "SSO 令牌已过期"}
        nbf = payload.get("nbf")
        if nbf is not None:
            try:
                if now + self.leeway < int(nbf):
                    return {"denied_reason": "SSO 令牌尚未生效(nbf)"}
            except (TypeError, ValueError):
                return {"denied_reason": "SSO nbf 声明非法"}

        if self.issuer and str(payload.get("iss", "")) != self.issuer:
            return {"denied_reason": "SSO issuer 不匹配"}
        if self.audience:
            aud = payload.get("aud")
            auds = aud if isinstance(aud, (list, tuple)) else ([aud] if aud is not None else [])
            if not any(str(a) in self.audience for a in auds):
                return {"denied_reason": "SSO audience 不匹配"}

        role = self._map_role(payload)
        if not role:
            return {"denied_reason": "SSO 角色声明缺失或无法映射(fail-closed, 不默认放行)"}

        tenant_id = _get_claim(payload, self.tenant_claim)
        tenant_id = str(tenant_id).strip() if tenant_id not in (None, "") else ""
        if not tenant_id and self.require_tenant:
            return {"denied_reason": "SSO 租户声明缺失(fail-closed)"}

        sub = payload.get("sub") or payload.get("preferred_username") or self.name
        return {
            "subject": "sso:%s" % str(sub)[:120],
            "role": role,
            "exp": exp,
            "auth": "sso",
            "tenant": tenant_id or None,       # 存在性由第1轮 tenant.resolve 校验(未知→403)
            "iss": str(payload.get("iss", "")),
        }


class LDAPProvider(Provider):
    """LDAP 适配器 —— **接口占位，未实现/未实测**（标准库无 LDAP 客户端，不引第三方依赖）。

    authenticate() 恒返回 denied_reason，绝不静默放行（fail-closed）。
    接入方式说明见文件末尾 & 报告「没做到/不确定项」。"""

    kind = "ldap"

    def __init__(self, name="ldap", *, url="", base_dn="", bind_dn="",
                 user_filter="(uid={user})", enabled=False, display_name=""):
        super().__init__(name, enabled=enabled, display_name=display_name or "LDAP(占位)")
        self.url = str(url or "")
        self.base_dn = str(base_dn or "")
        self.bind_dn = str(bind_dn or "")
        self.user_filter = str(user_filter or "")

    def status(self):
        st = super().status()
        st.update({"implemented": False, "url": self.url, "base_dn": self.base_dn,
                   "note": "接口占位：未实现/未实测（不引第三方依赖）"})
        return st

    def authenticate(self, token):
        return {"denied_reason": "LDAP 适配器为接口占位，未实现/未实测（不引第三方依赖）"}


# ── 注册表（注册 / 启用 / 停用 / 认证）────────────────────────────────────────
class SSORegistry(object):
    """SSO 适配器注册表：按注册顺序尝试**已启用**的适配器，第一个命中即返回。"""

    def __init__(self):
        self._providers = {}          # name -> Provider（保持插入顺序）

    def register(self, provider, enabled=None):
        """注册适配器（默认停用；enabled 显式给出则覆盖）。返回 provider。"""
        if enabled is not None:
            provider.enabled = bool(enabled)
        self._providers[provider.name] = provider
        return provider

    def unregister(self, name):
        return self._providers.pop(name, None) is not None

    def get(self, name):
        return self._providers.get(name)

    def enable(self, name):
        p = self._providers.get(name)
        if p is None:
            return False
        p.enabled = True
        return True

    def disable(self, name):
        p = self._providers.get(name)
        if p is None:
            return False
        p.enabled = False
        return True

    def providers(self):
        return list(self._providers.values())

    def enabled_providers(self):
        return [p for p in self._providers.values() if p.enabled]

    def is_enabled(self):
        return bool(self.enabled_providers())

    def authenticate(self, token):
        """按顺序尝试已启用适配器。

        返回 principal dict（命中）/ {"denied_reason":...}（已识别但被拒）/ None（都不是）。
        未启用任何适配器 → 恒 None（=「未配置 SSO」，api_server 行为逐字段不变）。
        """
        if not token:
            return None
        rejected = None
        for p in self._providers.values():
            if not p.enabled:
                continue
            try:
                r = p.authenticate(token)
            except Exception:
                r = None
            if r is None:
                continue
            if isinstance(r, dict) and r.get("denied_reason"):
                if rejected is None:
                    rejected = r
                continue
            return r
        return rejected

    def status(self):
        """观测（**不含任何密钥明文**）。"""
        return {
            "enabled": self.is_enabled(),
            "providers": [p.status() for p in self._providers.values()],
        }


# ── 配置装载（文件 + 环境变量）──────────────────────────────────────────────
def config_path(path=None):
    """配置路径：显式入参 > FOOD_SSO_CONFIG > 默认 codes/config/sso.json。"""
    if path:
        return os.path.abspath(path)
    return os.path.abspath(os.environ.get(ENV_CONFIG) or DEFAULT_CONFIG)


def load_config(path=None):
    """读 SSO 配置；缺失/损坏 → {}（等效「未配置」，老行为不变）。"""
    try:
        with open(config_path(path), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _env_flag(name):
    """三态：未设 → None；真值(1/true/yes/on) → True；其余 → False。"""
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _build_provider(name, cfg):
    """配置项 → Provider 实例。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    ptype = str(cfg.get("type", "oidc-jwt")).strip().lower()
    enabled = bool(cfg.get("enabled", False))
    if ptype in ("ldap", "ldaps"):
        return LDAPProvider(name, url=cfg.get("url", ""), base_dn=cfg.get("base_dn", ""),
                            bind_dn=cfg.get("bind_dn", ""), user_filter=cfg.get("user_filter", ""),
                            enabled=enabled, display_name=cfg.get("display_name", ""))
    return OIDCJWTProvider(
        name,
        alg=cfg.get("alg", "HS256"),
        secret=cfg.get("secret", ""),
        public_key_pem=cfg.get("public_key_pem", ""),
        issuer=cfg.get("issuer", ""),
        audience=cfg.get("audience"),
        role_claim=cfg.get("role_claim", "role"),
        role_map=cfg.get("role_map"),
        tenant_claim=cfg.get("tenant_claim", "tenant"),
        require_tenant=cfg.get("require_tenant", False),
        leeway=cfg.get("leeway", 0),
        enabled=enabled,
        display_name=cfg.get("display_name", ""),
    )


def from_env(path=None):
    """从配置 + 环境变量装载 SSORegistry。

    未配置（文件缺失且无 FOOD_SSO_HS256_SECRET）→ 空 registry（is_enabled()=False，
    authenticate 恒 None）→ 完全等价于「未接 SSO」，老部署行为逐字段不变。
    """
    reg = SSORegistry()
    doc = load_config(path)
    for name, cfg in (doc.get("providers") or {}).items():
        reg.register(_build_provider(name, cfg))

    # 便捷：无配置文件但给了共享密钥 → 合成一个 HS256 provider（测试/单机部署用）
    if not reg.providers() and os.environ.get("FOOD_SSO_HS256_SECRET"):
        reg.register(OIDCJWTProvider(
            "env-hs256",
            alg="HS256",
            secret=os.environ.get("FOOD_SSO_HS256_SECRET", ""),
            issuer=os.environ.get("FOOD_SSO_ISSUER", ""),
            audience=os.environ.get("FOOD_SSO_AUDIENCE", ""),
            role_claim=os.environ.get("FOOD_SSO_ROLE_CLAIM", "role"),
            tenant_claim=os.environ.get("FOOD_SSO_TENANT_CLAIM", "tenant"),
            require_tenant=(_env_flag("FOOD_SSO_REQUIRE_TENANT") or False),
            leeway=int(os.environ.get("FOOD_SSO_LEEWAY", "0") or 0),
            enabled=True,
        ))

    # 环境变量覆盖到指定 provider（默认第一个）
    target = os.environ.get("FOOD_SSO_PROVIDER") or ""
    prov = reg.get(target) if target else (reg.providers()[0] if reg.providers() else None)
    if prov is not None and isinstance(prov, OIDCJWTProvider):
        if os.environ.get("FOOD_SSO_HS256_SECRET"):
            prov.secret = os.environ.get("FOOD_SSO_HS256_SECRET", "")
        for env_name, attr in (("FOOD_SSO_ISSUER", "issuer"),
                               ("FOOD_SSO_ROLE_CLAIM", "role_claim"),
                               ("FOOD_SSO_TENANT_CLAIM", "tenant_claim")):
            if os.environ.get(env_name):
                setattr(prov, attr, os.environ.get(env_name))
        if os.environ.get("FOOD_SSO_AUDIENCE"):
            aud = os.environ.get("FOOD_SSO_AUDIENCE")
            prov.audience = [x.strip() for x in aud.split(",") if x.strip()]
        if os.environ.get("FOOD_SSO_LEEWAY"):
            try:
                prov.leeway = int(os.environ.get("FOOD_SSO_LEEWAY"))
            except ValueError:
                pass
        rt = _env_flag("FOOD_SSO_REQUIRE_TENANT")
        if rt is not None:
            prov.require_tenant = rt

    # 全局启用/停用
    forced = _env_flag("FOOD_SSO_ENABLED")
    if forced is not None:
        for p in reg.providers():
            p.enabled = forced
    elif not doc:
        # 无配置文件（例如仅 env 合成）：保持上面合成 provider 的默认 enabled
        pass
    return reg


def _main():
    """CLI：python codes/sso.py status|config —— 只读观测，不打印密钥。"""
    import sys
    reg = from_env()
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print("SSO 配置路径: %s" % config_path())
        print(json.dumps(reg.status(), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    print("\n当前: enabled=%s providers=%d (配置路径: %s)"
          % (reg.is_enabled(), len(reg.providers()), config_path()))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
