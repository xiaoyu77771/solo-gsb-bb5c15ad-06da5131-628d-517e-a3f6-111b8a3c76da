"""SQLAlchemy ORM 模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, String

from .database import Base


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False, default="未命名分析")
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    old_schema = Column(JSON, nullable=False)
    new_schema = Column(JSON, nullable=False)
    report = Column(JSON, nullable=False)
