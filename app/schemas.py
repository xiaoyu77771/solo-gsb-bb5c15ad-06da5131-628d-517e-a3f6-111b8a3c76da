"""Pydantic 请求/响应模型。"""

from datetime import datetime
from typing import Any, Dict, List, Union

from pydantic import BaseModel, Field

#: JSON Schema 可以是对象或布尔值
JsonSchema = Union[Dict[str, Any], bool]


class AnalysisCreate(BaseModel):
    """创建兼容性分析的请求体。"""

    name: str = Field(default="未命名分析", max_length=200, description="分析名称")
    old_schema: JsonSchema = Field(description="旧版 JSON Schema")
    new_schema: JsonSchema = Field(description="新版 JSON Schema")
    samples: List[Any] = Field(default_factory=list, description="可选的样例数据")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "用户服务 v1 → v2",
                    "old_schema": {
                        "type": "object",
                        "required": ["id", "name"],
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"},
                            "role": {"type": "string", "enum": ["admin", "user", "guest"]},
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
                        },
                    },
                    "samples": [
                        {"id": 1, "name": "Alice", "role": "guest"}
                    ],
                }
            ]
        }
    }


class AnalysisSummary(BaseModel):
    """列表接口返回的摘要信息。"""

    id: str
    name: str
    created_at: datetime
    backward_compatible: bool
    forward_compatible: bool


class AnalysisOut(BaseModel):
    """完整分析结果。"""

    id: str
    name: str
    created_at: datetime
    old_schema: JsonSchema
    new_schema: JsonSchema
    report: Dict[str, Any]
