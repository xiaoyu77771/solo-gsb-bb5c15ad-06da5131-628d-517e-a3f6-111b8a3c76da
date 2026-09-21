"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

import jsonschema
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from . import analyzer
from .database import Base, engine, get_db
from .models import Analysis
from .schemas import AnalysisCreate, AnalysisOut, AnalysisSummary


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="JSON Schema 兼容性分析 API",
    description=(
        "提交旧版与新版 JSON Schema，递归比较类型、必填字段、枚举、数组与组合关键字，"
        "分别判定向前/向后兼容，并给出最小反例路径与原因。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _check_schema_valid(schema, label: str) -> None:
    """校验提交的 schema 本身是合法的 JSON Schema。"""
    if isinstance(schema, bool):
        return
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{label} 不是合法的 JSON Schema: {exc}",
        )


def _to_out(record: Analysis) -> AnalysisOut:
    return AnalysisOut(
        id=record.id,
        name=record.name,
        created_at=record.created_at,
        old_schema=record.old_schema,
        new_schema=record.new_schema,
        report=record.report,
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/api/v1/analyses", response_model=AnalysisOut, status_code=201, tags=["analyses"])
def create_analysis(payload: AnalysisCreate, db: Session = Depends(get_db)):
    """创建一次兼容性分析：比较新旧 schema，返回完整报告。"""
    _check_schema_valid(payload.old_schema, "old_schema")
    _check_schema_valid(payload.new_schema, "new_schema")

    report = analyzer.analyze(payload.old_schema, payload.new_schema, payload.samples)
    record = Analysis(
        name=payload.name,
        old_schema=payload.old_schema,
        new_schema=payload.new_schema,
        report=report,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)


@app.get("/api/v1/analyses", response_model=list[AnalysisSummary], tags=["analyses"])
def list_analyses(db: Session = Depends(get_db)):
    """列出所有分析（按创建时间倒序）。"""
    rows = db.query(Analysis).order_by(Analysis.created_at.desc()).all()
    return [
        AnalysisSummary(
            id=r.id,
            name=r.name,
            created_at=r.created_at,
            backward_compatible=r.report.get("backward_compatible", False),
            forward_compatible=r.report.get("forward_compatible", False),
        )
        for r in rows
    ]


@app.get("/api/v1/analyses/{analysis_id}", response_model=AnalysisOut, tags=["analyses"])
def get_analysis(analysis_id: str, db: Session = Depends(get_db)):
    """按 ID 查询完整分析报告。"""
    record = db.get(Analysis, analysis_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"分析 {analysis_id} 不存在")
    return _to_out(record)
