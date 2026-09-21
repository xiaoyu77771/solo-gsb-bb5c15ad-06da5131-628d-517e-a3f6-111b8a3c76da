# JSON Schema 兼容性分析 API

基于 **FastAPI + SQLite** 的 JSON Schema 演进兼容性分析服务。提交旧版与新版 schema（及可选样例数据），服务会递归比较类型、必填字段、枚举、数组与组合关键字（`allOf` / `anyOf` / `oneOf`），分别判定**向前兼容**与**向后兼容**，并为每个破坏性变更生成**最小反例路径与原因**。

## 兼容性定义

| 方向 | 含义 | 判定条件 |
| --- | --- | --- |
| 向后兼容（backward） | 新 schema 能读取旧数据 | 旧 schema 接受的实例 ⊆ 新 schema 接受的实例 |
| 向前兼容（forward） | 旧 schema 能读取新数据 | 新 schema 接受的实例 ⊆ 旧 schema 接受的实例 |

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

- 服务端口：**8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在名为 `schema-data` 的 Docker volume 中（容器内路径 `/app/data/analyses.db`）。

## 示例请求

### 1. 创建分析

```bash
curl -X POST http://localhost:8000/api/v1/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "name": "用户服务 v1 → v2",
    "old_schema": {
      "type": "object",
      "required": ["id", "name"],
      "properties": {
        "id":   {"type": "integer"},
        "name": {"type": "string"},
        "role": {"type": "string", "enum": ["admin", "user", "guest"]},
        "tags": {"type": "array", "items": {"type": ["string", "integer"]}}
      }
    },
    "new_schema": {
      "type": "object",
      "required": ["id", "name", "email"],
      "properties": {
        "id":    {"type": "integer"},
        "name":  {"type": "string"},
        "email": {"type": "string"},
        "role":  {"type": "string", "enum": ["admin", "user"]},
        "tags":  {"type": "array", "items": {"type": "string"}}
      }
    },
    "samples": [
      {"id": 1, "name": "Alice", "role": "guest", "tags": ["vip", 42]}
    ]
  }'
```

响应（`201 Created`，节选）：

```json
{
  "id": "82e7b1b0-0be6-439b-8131-a64773ab9904",
  "name": "用户服务 v1 → v2",
  "report": {
    "backward_compatible": false,
    "forward_compatible": true,
    "summary": "仅向前兼容：旧 schema 可读取新数据，但新 schema 无法读取全部旧数据",
    "backward_violations": [
      {
        "path": "$.email",
        "rule": "required",
        "reason": "acceptor 将属性 'email' 标记为必填，而 source 允许其缺失",
        "counterexample": {"id": 0, "name": ""}
      },
      {
        "path": "$.role",
        "rule": "enum",
        "reason": "source 允许的枚举值 \"guest\" 不在 acceptor 的枚举范围内",
        "counterexample": "guest"
      },
      {
        "path": "$.tags[*]",
        "rule": "type",
        "reason": "source 允许类型 'integer'，acceptor 只允许 ['string']",
        "counterexample": 0
      }
    ],
    "forward_violations": [],
    "sample_results": [
      {
        "index": 0,
        "valid_against_old": true,
        "valid_against_new": false,
        "old_errors": [],
        "new_errors": ["<root>: 'email' is a required property", "..."]
      }
    ],
    "notes": []
  }
}
```

### 2. 查询报告

```bash
# 列表（摘要）
curl http://localhost:8000/api/v1/analyses

# 按 ID 查询完整报告
curl http://localhost:8000/api/v1/analyses/82e7b1b0-0be6-439b-8131-a64773ab9904
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/analyses` | 创建分析（同步执行，返回完整报告） |
| `GET` | `/api/v1/analyses` | 列出所有分析（摘要，按创建时间倒序） |
| `GET` | `/api/v1/analyses/{id}` | 按 ID 查询完整报告（不存在返回 404） |

请求体字段：`name`（可选）、`old_schema`、`new_schema`（JSON Schema 对象或布尔值）、`samples`（可选样例数组）。提交的 schema 会先经过 JSON Schema 元校验，非法 schema 返回 `422`。

## 支持的检查项

- **类型**：`type` 收窄/放宽（含 `integer` ⊆ `number` 的子类型关系）
- **必填字段**：`required` 新增/移除
- **枚举/常量**：`enum`、`const` 集合包含关系
- **对象**：`properties` 递归比较、`additionalProperties`（布尔或 schema）、`minProperties` / `maxProperties`
- **数组**：`items` 递归比较、`minItems` / `maxItems`、`uniqueItems`
- **组合关键字**：`allOf`（source 侧合并、acceptor 侧逐约束）、`anyOf` / `oneOf`（并集包含判定）
- **数值/字符串**：`minimum` / `maximum` / `exclusiveMinimum` / `exclusiveMaximum`、`minLength` / `maxLength` / `pattern`
- **本地引用**：`$ref: "#/..."`（JSON Pointer，带循环保护）
- **样例数据**：逐条对照新旧 schema 校验，给出具体错误位置

每条违规包含：`path`（JSON 路径，如 `$.tags[*]`）、`rule`（触发的规则）、`reason`（中文原因说明）、`counterexample`（被 source 接受但被 acceptor 拒绝的最小实例）。

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                          # 运行测试（32 个用例）
uvicorn app.main:app --reload   # 本地启动，端口 8000
```

默认数据库为 `./data/analyses.db`，可用环境变量 `DATABASE_URL` 覆盖（如 `sqlite:////tmp/test.db`）。

## 项目结构

```
app/
├── main.py        # FastAPI 入口与路由
├── analyzer.py    # 兼容性分析核心（递归比较 + 最小反例生成）
├── database.py    # SQLite / SQLAlchemy 连接
├── models.py      # ORM 模型
└── schemas.py     # Pydantic 请求/响应模型
tests/
├── test_analyzer.py   # 分析器单元测试
└── test_api.py        # API 接口测试（内存 SQLite）
Dockerfile
docker-compose.yml
```

## 限制说明

- 分析为**结构化启发式比较**：`not`、`if`/`then`/`else`、`patternProperties`、`contains`、`format`、`multipleOf` 等关键字不参与比较，出现时会在报告的 `notes` 中提示。
- `oneOf` 在 acceptor 侧按 `anyOf` 近似处理。
- 仅支持本地 `$ref`（`#/...`），外部引用按无约束处理。
