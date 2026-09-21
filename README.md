# JSON Schema 兼容性分析 API

用 **FastAPI + SQLite** 构建的 JSON Schema 兼容性分析服务。提交旧版与新版 JSON Schema
（以及可选的样例文档），服务会递归比较两者的**类型、必填字段、枚举、数组、对象和组合关键字**，
分别判断**向后兼容**与**向前兼容**，并为每一处破坏点生成**最小反例路径、反例文档和原因说明**。

- Web 框架：FastAPI
- 持久化：SQLite + SQLAlchemy
- Schema 校验：jsonschema（自动识别 draft-04 / 06 / 07 / 2019-09 / 2020-12 写法）
- 自动生成 OpenAPI 文档：`/docs`（Swagger UI）

---

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由：创建分析、查询报告
│   ├── compat.py        # 兼容性分析引擎（递归比较 + 最小反例生成）
│   ├── database.py      # SQLite / SQLAlchemy 初始化
│   ├── models.py        # Analysis ORM 模型
│   └── schemas.py       # Pydantic 请求 / 响应模型
├── tests/
│   ├── test_compat.py   # 引擎单元测试（30+ 用例）
│   └── test_api.py      # 接口端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

服务启动后监听 **`http://localhost:8000`**：

| 用途          | 地址                                   |
| ------------- | -------------------------------------- |
| API 根路径    | http://localhost:8000/  （跳转 /docs） |
| 健康检查      | http://localhost:8000/health          |
| Swagger 文档  | http://localhost:8000/docs            |
| OpenAPI JSON  | http://localhost:8000/openapi.json    |

SQLite 数据文件位于容器内 `/app/data/schema_compat.db`，通过命名卷
`schema_compat_data` 持久化，容器重建后历史报告仍在。

---

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可通过环境变量覆盖数据库位置：

```bash
DATABASE_URL="sqlite:///./data/schema_compat.db" uvicorn app.main:app
```

运行测试：

```bash
pytest -q
```

---

## 兼容性判定模型

设 `Old(S)` 表示旧 schema 接受的全部实例，`New(S)` 表示新 schema 接受的全部实例。

| 方向 | 定义 | 含义 | 反例 |
| ---- | ---- | ---- | ---- |
| **向后兼容** `backward` | `New ⊇ Old` | 新版能读取所有旧数据 | 旧版合法、**新版拒绝**的文档 |
| **向前兼容** `forward` | `Old ⊇ New` | 旧版能读取所有新数据 | 新版合法、**旧版拒绝**的文档 |

典型经验：

- 新增**可选**字段：两个方向都兼容；
- 新增**必填**字段：破坏向后兼容；
- **删除**必填字段：破坏向前兼容；
- 收窄枚举 / 收紧数值范围 / 提高 minItems：破坏向后兼容；
- 类型直接改变（string ↔ integer）：两个方向都破坏。

### 检查覆盖的关键字

- `type`（含 integer/number 关系、无 type 时的结构推断）
- `required`、`properties`、`additionalProperties`、`patternProperties`、
  `minProperties` / `maxProperties`
- `enum`、`const`
- `items`（齐次数组）、`prefixItems` / draft-04 列表形式 `items`（元组）、
  `additionalItems`、`minItems` / `maxItems`、`uniqueItems`
- `minimum` / `maximum`、draft-04 布尔型 `exclusiveMinimum/Maximum`、
  `exclusiveMinimum/Maximum` 数值写法、`multipleOf`
- `minLength` / `maxLength`、`pattern`
- 组合关键字：`allOf`（浅合并后比较）、`anyOf`、`oneOf`、`not`
- 文档内部 `$ref`（`#/$defs/...`，带环保护）

### 反例的可信度

每一个反例在返回前都会经过真实的 `jsonschema` 校验，确保**满足 source schema
且确实被 target schema 拒绝**，不会返回想当然的假反例。

- `severity: error`：找到了经过验证的最小反例，确定破坏兼容；
- `severity: warning`：启发式规则（如 `pattern` 变更、`multipleOf` 新增、
  `anyOf` 新增）无法自动构造出经过验证的反例，标记为潜在风险供人工确认。

`path` 使用 JSON Pointer 风格定位到 schema 中出问题的关键字位置（如
`#/properties/addresses/items`）；`counterexample_path` 定位反例文档中
触发问题的值位置（如 `#/addresses/0`）。

---

## API

### `POST /api/analyses` — 创建兼容性分析

请求体：

| 字段        | 类型    | 必填 | 说明                       |
| ----------- | ------- | ---- | -------------------------- |
| `name`      | string  | 否   | 本次分析的名称             |
| `old_schema`| object  | 是   | 旧版 JSON Schema           |
| `new_schema`| object  | 是   | 新版 JSON Schema           |
| `samples`   | array   | 否   | 真实样例文档，用于精准定位 |

```bash
curl -X POST http://localhost:8000/api/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "name": "user v1 -> v2",
    "old_schema": {
      "type": "object",
      "properties": {
        "id":   {"type": "integer"},
        "role": {"type": "string", "enum": ["admin", "editor", "viewer"]}
      },
      "required": ["id", "role"]
    },
    "new_schema": {
      "type": "object",
      "properties": {
        "id":    {"type": "string"},
        "role":  {"type": "string", "enum": ["admin", "editor"]},
        "email": {"type": "string"}
      },
      "required": ["id", "role", "email"]
    },
    "samples": [
      {"id": 42, "role": "viewer"}
    ]
  }'
```

响应（节选）：

```json
{
  "id": 1,
  "name": "user v1 -> v2",
  "created_at": "2026-09-21T15:00:00+00:00",
  "report": {
    "backward_compatible": false,
    "forward_compatible": false,
    "summary": {
      "backward": { "errors": 4, "warnings": 0, "total": 4 },
      "forward":  { "errors": 1, "warnings": 0, "total": 1 },
      "total": 5, "errors": 5, "warnings": 0
    },
    "issues": [
      {
        "direction": "backward",
        "path": "#/properties/role",
        "code": "enum_value_removed",
        "severity": "error",
        "message": "枚举值 \"viewer\" 在新版中已被移除",
        "reason": "旧版枚举包含 \"viewer\"，新版枚举不再包含该值，使用该值的旧数据会校验失败",
        "counterexample": { "id": 0, "role": "viewer" },
        "counterexample_path": "#/role"
      },
      {
        "direction": "backward",
        "path": "#/properties/id",
        "code": "type_removed",
        "severity": "error",
        "message": "新版不再接受类型 integer",
        "reason": "旧版允许类型 integer，新版仅允许 ['string']，该类型的旧数据会校验失败",
        "counterexample": { "id": 0, "role": "admin" },
        "counterexample_path": "#/id"
      }
    ],
    "sample_results": [
      { "index": 0, "valid_against_old": true, "valid_against_new": false,
        "direction": "backward" }
    ]
  }
}
```

提交的 schema 本身不合法时返回 `400`：

```json
{ "detail": { "field": "old_schema", "message": "非法 JSON Schema: ..." } }
```

### `GET /api/analyses` — 列出分析（摘要）

支持 `limit`（默认 50，最大 200）和 `offset` 分页：

```bash
curl "http://localhost:8000/api/analyses?limit=20"
```

```json
{
  "total": 1,
  "items": [
    {
      "id": 1,
      "name": "user v1 -> v2",
      "created_at": "2026-09-21T15:00:00+00:00",
      "backward_compatible": false,
      "forward_compatible": false,
      "errors": 5,
      "warnings": 0
    }
  ]
}
```

### `GET /api/analyses/{id}` — 获取完整报告

返回与 `POST` 完全相同的完整结构（含两个 schema、样例结果与全部 issues）：

```bash
curl http://localhost:8000/api/analyses/1
```

记录不存在返回 `404`。

### `GET /health`

```json
{ "status": "ok" }
```

---

## issue 代码一览

| code | 触发场景 |
| ---- | -------- |
| `type_removed` / `type_restricted` | 类型被移除或收窄 |
| `required_added` | 字段在新版变为必填 |
| `enum_value_removed` / `enum_added` | 枚举值被删除 / 新增枚举约束 |
| `const_added` / `const_changed` | 新增常量约束 / 常量值变化 |
| `property_no_longer_allowed` | 属性被删除且不允许额外字段 |
| `additional_properties_forbidden` | `additionalProperties` 收紧为 false |
| `min/max_properties_*` | 对象属性数量边界变化 |
| `min/max_items_*` | 数组长度边界变化 |
| `unique_items_added` | 新增元素唯一性要求 |
| `minimum_increased` / `maximum_decreased` | 数值边界收紧 |
| `multiple_of_added` / `integer_required` | 新增整除 / 整数要求 |
| `min/max_length_*` / `pattern_changed` | 字符串长度、正则变化 |
| `anyOf/oneOf_branch_missing` / `_added` / `_rejected` | 组合分支不兼容 |
| `not_added` | 新增 not 否定约束 |
| `schema_forbids_all` | 目标位置为 `false`，拒绝所有实例 |
| `sample_rejected_by_new/old` | 提供的真实样例被某一版拒绝 |

---

## 实现说明

引擎核心是单向蕴含检查 `find_violations(source, target)`：递归找出所有
"source 允许、target 拒绝"的结构，为每个结构生成一个位于该路径的最小片段，
再沿 schema 路径反向包裹成完整文档（过程中自动补齐 `required`、数组前缀、
`minItems` 等约束），最后用 `jsonschema` 双向验证。交换入参即可得到另一个
兼容方向，所以所有规则只需实现一遍。
