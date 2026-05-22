from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.database import Base


class SystemSetting(Base):
    """Key-value store for runtime config overrides (set from UI, override env vars)."""
    __tablename__ = "system_settings"

    key        = Column(String(100), primary_key=True)
    value      = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
