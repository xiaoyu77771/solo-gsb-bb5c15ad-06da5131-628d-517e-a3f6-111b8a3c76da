"""分析记录的 ORM 模型。"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Analysis(Base):
    """一次 JSON Schema 兼容性分析的完整快照。"""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    old_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    new_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    samples: Mapped[list | None] = mapped_column(JSON, nullable=True)
    report: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "old_schema": self.old_schema,
            "new_schema": self.new_schema,
            "samples": self.samples or [],
            "report": self.report,
        }
