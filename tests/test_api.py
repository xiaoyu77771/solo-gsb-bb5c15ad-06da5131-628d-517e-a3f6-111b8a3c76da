"""API 接口测试（使用内存 SQLite）。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

PAYLOAD = {
    "name": "用户服务 v1 → v2",
    "old_schema": {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "role": {"type": "string", "enum": ["admin", "user", "guest"]},
            "tags": {"type": "array", "items": {"type": ["string", "integer"]}},
        },
    },
    "new_schema": {
        "type": "object",
        "required": ["id", "name", "email"],
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "email": {"type": "string"},
            "role": {"type": "string", "enum": ["admin", "user"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    },
    "samples": [
        {"id": 1, "name": "Alice", "role": "guest", "tags": ["vip", 42]},
        {"id": 2, "name": "Bob", "email": "bob@example.com", "role": "admin", "tags": ["vip"]},
    ],
}


@pytest.fixture(autouse=True)
def clean_db():
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_analysis():
    resp = client.post("/api/v1/analyses", json=PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == PAYLOAD["name"]
    report = body["report"]
    # 新增必填 email、枚举去掉 guest、数组元素去掉 integer → 向后不兼容
    assert report["backward_compatible"] is False
    # 新数据仍能被旧 schema 接受 → 向前兼容
    assert report["forward_compatible"] is True
    rules = {(v["path"], v["rule"]) for v in report["backward_violations"]}
    assert ("$.email", "required") in rules
    assert ("$.role", "enum") in rules
    assert ("$.tags[*]", "type") in rules
    # 样例校验
    samples = report["sample_results"]
    assert samples[0]["valid_against_old"] is True
    assert samples[0]["valid_against_new"] is False
    assert samples[1]["valid_against_new"] is True


def test_get_analysis():
    created = client.post("/api/v1/analyses", json=PAYLOAD).json()
    resp = client.get(f"/api/v1/analyses/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    assert resp.json()["report"]["summary"]


def test_get_analysis_not_found():
    resp = client.get("/api/v1/analyses/does-not-exist")
    assert resp.status_code == 404


def test_list_analyses():
    client.post("/api/v1/analyses", json=PAYLOAD)
    client.post("/api/v1/analyses", json={**PAYLOAD, "name": "第二次"})
    resp = client.get("/api/v1/analyses")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert {"id", "name", "created_at", "backward_compatible", "forward_compatible"} <= set(items[0])


def test_invalid_schema_rejected():
    resp = client.post("/api/v1/analyses", json={
        "old_schema": {"type": "not-a-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 422
    assert "old_schema" in resp.json()["detail"]


def test_missing_field_rejected():
    resp = client.post("/api/v1/analyses", json={"old_schema": {}})
    assert resp.status_code == 422
