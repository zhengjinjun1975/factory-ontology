#!/usr/bin/env python3
"""CWE-862 回归: 本体数据端点必须默认受保护(不依赖 FOOD_STRICT_AUTH 灰度开关)。

运行: cd codes && python -m pytest tests/test_ontology_auth.py -v
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
sys.path.insert(0, ROOT)

# fail-closed 鉴权: 测试需配置 key + 传 X-API-Key(与 test_api.py 一致的约定)。
os.environ.setdefault("FOOD_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("FOOD_READ_KEY", "test-read-key")

import pytest

_ONTOLOGY_ENDPOINTS = [
    "/api/ontology/structure",
    "/api/ontology/graph",
    "/api/ontology/graph-svg",
]


@pytest.mark.parametrize("endpoint", _ONTOLOGY_ENDPOINTS)
def test_ontology_endpoints_require_authentication(endpoint):
    """安全不变量: 本体三条端点(structure/graph/graph-svg)——

    - 匿名访问 → 401
    - 错误凭据 → 401
    - 有效 read 凭据 → 200

    且不受 FOOD_STRICT_AUTH 灰度开关影响(该开关默认关, 用于兼容其它匿名端点)。
    """
    from fastapi.testclient import TestClient
    import api_server as api

    client = TestClient(api.app)

    resp = client.get(endpoint)
    assert resp.status_code == 401, (
        f"安全回归: {endpoint} 匿名可访问, 应 401 实得 {resp.status_code}"
    )

    resp = client.get(endpoint, headers={"X-API-Key": "invalid-key"})
    assert resp.status_code == 401, (
        f"安全回归: {endpoint} 错误凭据未被拒绝, 应 401 实得 {resp.status_code}"
    )

    resp = client.get(
        endpoint, headers={"X-API-Key": os.environ["FOOD_READ_KEY"]}
    )
    assert resp.status_code == 200, (
        f"{endpoint} 携带有效 read 凭据应可访问, 实得 {resp.status_code}: {resp.text}"
    )


def test_health_still_anonymous():
    """回归护栏: 修复本体端点鉴权不应牵连 /health(探活/负载均衡必须匿名可访问)。"""
    from fastapi.testclient import TestClient
    import api_server as api

    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
