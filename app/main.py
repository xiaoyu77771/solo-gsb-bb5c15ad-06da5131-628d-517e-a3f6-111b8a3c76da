"""FastAPI 入口：创建分析、查询报告。"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import compat
from app.database import get_db, init_db
from app.models import Analysis
from app.schemas import (
    AnalysisCreate,
    AnalysisListResponse,
    AnalysisResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="JSON Schema 兼容性分析 API",
    description=(
        "递归比较旧版 / 新版 JSON Schema 的类型、必填字段、枚举、数组与组合关键字，"
        "分别判断向后 / 向前兼容性，并生成最小反例路径与原因。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post(
    "/api/analyses",
    response_model=AnalysisResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["analyses"],
    summary="创建兼容性分析",
)
def create_analysis(payload: AnalysisCreate, db: Session = Depends(get_db)):
    # 校验两个 schema 本身是合法的 JSON Schema
    for label, schema in (("old_schema", payload.old_schema), ("new_schema", payload.new_schema)):
        try:
            compat.make_validator(schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"field": label, "message": f"非法 JSON Schema: {exc.message}"},
            )

    report = compat.analyze(payload.old_schema, payload.new_schema, payload.samples)

    record = Analysis(
        name=payload.name,
        old_schema=payload.old_schema,
        new_schema=payload.new_schema,
        samples=payload.samples or [],
        report=report,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.to_dict()


@app.get(
    "/api/analyses",
    response_model=AnalysisListResponse,
    tags=["analyses"],
    summary="列出全部分析（摘要）",
)
def list_analyses(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    rows = db.scalars(
        select(Analysis).order_by(Analysis.id.desc()).limit(limit).offset(offset)
    ).all()
    items = [
        {
            "id": row.id,
            "name": row.name,
            "created_at": row.created_at.isoformat(),
            "backward_compatible": row.report["backward_compatible"],
            "forward_compatible": row.report["forward_compatible"],
            "errors": row.report["summary"]["errors"],
            "warnings": row.report["summary"]["warnings"],
        }
        for row in rows
    ]
    return {"total": len(items), "items": items}


@app.get(
    "/api/analyses/{analysis_id}",
    response_model=AnalysisResponse,
    tags=["analyses"],
    summary="获取单个分析的完整报告",
)
def get_analysis(analysis_id: int, db: Session = Depends(get_db)):
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分析记录不存在")
    return row.to_dict()
