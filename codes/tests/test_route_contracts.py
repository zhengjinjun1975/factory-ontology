#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""余下路由族的行为契约回归门（为 P1-1 拆分收口而建，2026-10-02）。

口径（有意如此，别扩成"什么都测"）：
  * **读端点**：真调，钉 `status` + 响应**顶层键集合**（键集合变了 = 契约变了，立刻红）。
  * **写端点**：只钉两类**不落盘**的行为 —— ① 鉴权 fail-closed（缺 key → 401）；
    ② 请求校验（缺必填 → 422/400）与**未知 kb 拒绝**。
    真实写入路径（上传/重建/确认落库/快照）不在这里重复调，否则每次跑测试都污染数据目录；
    那部分由 `codes/e2e_test.py` 与手工联调覆盖。
  * 基线由 `contracts` 表内嵌（出自真跑一次采集，不是手写），逐条回放比对。

运行: cd codes && python -m pytest tests/test_route_contracts.py -q
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # codes/
sys.path.insert(0, ROOT)

# 与 test_api.py 同源：fail-closed 鉴权需要先把 key 配好再 import api_server
os.environ.setdefault("FOOD_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("FOOD_TOKEN_SECRET", "test-token-secret-0123456789abcdef")

HEADERS = {"X-API-Key": os.environ["FOOD_ADMIN_KEY"]}

# id, method, path, auth(None/"key"/"admin"), payload, 期望状态(可多值), 期望顶层键, 说明
CONTRACTS = [
    ("root_health", "GET", "/health", None, None, (200,), ['status', 'version']),
    ("root_metrics", "GET", "/metrics", None, None, (200,), ['ok', 'requests', 'total']),
    ("root_home", "GET", "/", None, None, (200,), None),
    ("root_admin_page", "GET", "/admin", None, None, (200,), None),
    ("version_get", "GET", "/api/version", 'key', None, (200,), ['data', 'elapsed_s', 'ok']),
    ("kbs_list", "GET", "/api/kbs", 'key', None, (200,), ['active', 'kbs', 'ok', 'tenant', 'total']),
    ("kb_active_get", "GET", "/api/kb/active", 'key', None, (200,), ['file', 'kb', 'ok', 'state']),
    ("kb_active_set_missing_body", "POST", "/api/kb/active", 'key', {}, (422,), ['detail']),
    ("kb_active_set_bad_kb", "POST", "/api/kb/active", 'key', {'kb': '__no_such_kb__'}, (200,), ['error', 'ok']),
    ("kb_active_after_bad", "GET", "/api/kb/active", 'key', None, (200,), ['file', 'kb', 'ok', 'state']),
    ("kb_lexicon_export", "GET", "/api/kb/food/lexicon/export", 'key', None, (200,), ['attr_cn2en', 'attr_en2cn', 'description', 'field_aliases', 'status_cn2en', 'type_cn2en', 'value_fields']),
    ("kb_lexicon_import_missing_body", "POST", "/api/kb/food/lexicon/import", 'key', None, (200,), ['error', 'ok']),
    ("app_config", "GET", "/api/app-config", 'key', None, (200,), ['examples', 'icon', 'kb', 'name', 'ok']),
    ("scan_batch", "GET", "/api/scan?code=B001", 'key', None, (200,), ['batch', 'code', 'ok', 'produce_date', 'product', 'raw_materials']),
    ("admin_kbs", "GET", "/api/admin/kbs", 'admin', None, (200,), ['active', 'kbs', 'ok', 'tenant', 'total']),
    ("admin_audit", "GET", "/api/admin/audit?limit=3", 'admin', None, (200,), ['audit', 'count', 'ok']),
    ("admin_audit_limit_bad", "GET", "/api/admin/audit?limit=abc", 'admin', None, (422,), ['detail']),
    ("admin_rebuild_noauth", "POST", "/api/admin/rebuild", None, None, (401,), ['detail']),
    ("admin_sync_noauth", "POST", "/api/admin/sync", None, None, (401,), ['detail']),
    ("admin_upload_noauth", "POST", "/api/admin/upload", None, None, (401,), ['detail']),
    ("ontology_structure", "GET", "/api/ontology/structure?kb=food", 'key', None, (200,), ['classes', 'instance_total', 'kb', 'nt_file', 'object_properties', 'ok', 'subclass_of']),
    ("ontology_graph", "GET", "/api/ontology/graph?kb=food", 'key', None, (200,), ['counts', 'edges', 'nodes', 'ok']),
    ("ontology_graph_svg", "GET", "/api/ontology/graph-svg", 'key', None, (200,), None),
    ("ontology_build_missing_body", "POST", "/api/ontology/build", 'key', {}, (422,), ['detail']),
    ("ontology_build_noauth", "POST", "/api/ontology/build", None, {'kb': 'x'}, (401,), ['detail']),
    ("ontology_suggest_missing_body", "POST", "/api/ontology/suggest", 'key', {}, (422,), ['detail']),
    ("ontology_confirm_missing_body", "POST", "/api/ontology/confirm", 'key', {}, (422,), ['detail']),
    ("ontology_confirm_bad_schema", "POST", "/api/ontology/confirm", 'key', {'kb': 'food', 'schema': {'entities': 'not-a-list'}}, (200,), ['elapsed_s', 'error', 'ok']),
    ("ontology_self_onboard_noauth", "POST", "/api/ontology/self-onboard", None, None, (401,), ['detail']),
    ("assets_list", "GET", "/api/assets/list?kb=food", 'key', None, (200,), ['data', 'elapsed_s', 'ok']),
    ("assets_snapshot_bad_kb", "POST", "/api/assets/snapshot", 'key', {'kb': '__no_such_kb__'}, (200,), ['elapsed_s', 'error', 'ok']),
    ("assets_rollback_noauth", "POST", "/api/assets/rollback", None, {'kb': 'food'}, (401,), ['detail']),
    ("kbs_examples_noauth", "POST", "/api/kbs/food/examples", None, {}, (401,), ['detail']),
    ("ask_food", "POST", "/api/ask", 'key', {'question': '乳制品的数量', 'kb': 'food'}, (200,), ['advisory', 'answer', 'confidence', 'engines', 'evidence', 'evidence_trace', 'hit', 'kb', 'mode', 'no_basis', 'ok', 'question', 'reason', 'structured']),
    ("ask_miss", "POST", "/api/ask", 'key', {'question': '完全无关xyz', 'kb': 'food'}, (200,), ['advisory', 'answer', 'confidence', 'engines', 'evidence', 'evidence_trace', 'hit', 'kb', 'mode', 'no_basis', 'ok', 'question', 'reason', 'structured']),
    ("ask_noauth", "POST", "/api/ask", None, {'question': '乳制品的数量'}, (401,), ['detail']),
    ("auth_state", "GET", "/api/auth/state", 'admin', None, (200,), ['active_bans', 'ban_rows', 'config', 'consumed_rows', 'db_bytes', 'db_path', 'fail_rows', 'ok', 'refresh_rows', 'revoked_rows', 'state_dir', 'tokens_rows']),
    ("auth_sso_status", "GET", "/api/auth/sso/status", 'admin', None, (200,), ['config_path', 'enabled', 'ok', 'providers']),
    ("auth_whoami", "GET", "/api/auth/whoami", 'key', None, (200,), ['auth', 'exp', 'expires_at', 'ok', 'role', 'subject']),
    ("auth_token_noauth", "POST", "/api/auth/token", None, {'subject': 't', 'role': 'viewer'}, (401,), ['detail']),
    ("auth_refresh_bad", "POST", "/api/auth/refresh", 'key', {'refresh_token': 'bogus.token.value'}, (401, 403, 429), ['detail']),
    ("auth_revoke_missing_body", "POST", "/api/auth/revoke", 'admin', {}, (400,), ['detail']),
    ("auth_ban_unban", "POST", "/api/auth/ban", 'admin', {'subject': '__contract_probe__', 'minutes': 1}, (200,), ['banned', 'ok', 'until']),
    ("auth_unban_probe", "POST", "/api/auth/unban", 'admin', {'subject': '__contract_probe__'}, (200,), ['cleared', 'ok', 'unban']),
]


def _client():
    from fastapi.testclient import TestClient
    import api_server as api
    return TestClient(api.app)


@pytest.fixture(scope="module", autouse=True)
def _restore_active_kb():
    """跑完把激活 kb 恢复成进模块前的值。

    没有这道守卫，一旦某条用例真的把激活态写坏（本文件曾实测发生过：变异测试删掉校验那次，
    激活 kb 被写成 __no_such_kb__，随后 e2e 的问答三项全红），测试自己就成了环境污染源。
    """
    import api_server as api
    before = api._active_kb()
    yield
    try:
        if api._active_kb() != before and before in api.KBS:
            api._set_active_kb(before, source="switch")
    except Exception:  # noqa: BLE001
        pass


def _call(c, method, path, auth, payload):
    headers = dict(HEADERS) if auth in ("key", "admin") else {}
    kwargs = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload
    return getattr(c, method.lower())(path, **kwargs)


@pytest.mark.parametrize("cid,method,path,auth,payload,statuses,keys", CONTRACTS,
                         ids=[c[0] for c in CONTRACTS])
def test_endpoint_contract(cid, method, path, auth, payload, statuses, keys):
    r = _call(_client(), method, path, auth, payload)
    assert r.status_code in statuses, "%s: 期望 %s 实得 %s | %s" % (cid, statuses, r.status_code, r.text[:200])
    if keys is not None:
        got = sorted(r.json().keys())
        assert got == keys, "%s: 响应顶层键变了\n  期望 %s\n  实得 %s" % (cid, keys, got)


def test_unknown_kb_is_rejected_and_does_not_switch():
    """未知 kb 必须被拒且**不落盘**（2026-10-02 契约测试发现的静默切换 bug 的守卫门）。"""
    c = _client()
    before = c.get("/api/kb/active", headers=HEADERS).json()["kb"]
    r = c.post("/api/kb/active", json={"kb": "__no_such_kb__"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is False, r.text
    after = c.get("/api/kb/active", headers=HEADERS).json()["kb"]
    assert after == before, "未知 kb 竟然改动了激活态: %s -> %s" % (before, after)


def test_token_lifecycle_issue_whoami_revoke():
    """令牌全生命周期（签发 → 使用 → 吊销 → 失效），reversible，不污染状态。"""
    c = _client()
    r = c.post("/api/auth/token", json={"role": "read", "ttl_seconds": 120, "revocable": True}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    token, jti = body.get("token"), body.get("jti")
    assert token and jti, body
    h = {"Authorization": "Bearer %s" % token}
    w1 = c.get("/api/auth/whoami", headers=h)
    assert w1.status_code == 200 and w1.json().get("ok") is True, w1.text
    rv = c.post("/api/auth/revoke", json={"jti": jti, "reason": "contract-test"}, headers=HEADERS)
    assert rv.status_code in (200, 400), rv.text
    w2 = c.get("/api/auth/whoami", headers=h)
    assert w2.status_code == 401, "吊销后令牌仍可用: %s %s" % (w2.status_code, w2.text[:200])
