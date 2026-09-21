"""FastAPI 接口测试（TestClient + 临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_analysis(client):
    resp = client.post("/api/analyses", json={
        "name": "user-schema-v1-v2",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    analysis_id = data["id"]
    assert data["report"]["backward_compatible"] is False
    assert data["report"]["forward_compatible"] is True
    assert data["report"]["summary"]["errors"] >= 1

    # 反例字段存在
    issues = data["report"]["issues"]
    backward = [i for i in issues if i["direction"] == "backward"]
    assert backward
    assert any(i["counterexample"] is not None for i in backward)

    # 样例结果
    assert data["report"]["sample_results"][0]["valid_against_new"] is False

    # 查询报告
    resp2 = client.get(f"/api/analyses/{analysis_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == analysis_id

    # 列表
    resp3 = client.get("/api/analyses")
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


def test_get_missing_returns_404(client):
    resp = client.get("/api/analyses/999999")
    assert resp.status_code == 404


def test_invalid_schema_returns_400(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "not-a-real-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "old_schema"


def test_compatible_schemas(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "new_schema": {"type": "object",
                       "properties": {"a": {"type": "string"},
                                      "b": {"type": "integer"}}},
    })
    assert resp.status_code == 201
    report = resp.json()["report"]
    assert report["backward_compatible"]
    assert report["forward_compatible"]
    assert report["summary"]["errors"] == 0
