from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from app.database import Base


class UserActivityLog(Base):
    """Audit trail of user actions — login, article generation, pipeline runs, etc."""
    __tablename__ = "user_activity_logs"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    shop_domain   = Column(String(255), nullable=True, index=True)
    action        = Column(String(100), nullable=False)          # login | generate_article | run_pipeline | run_audit | publish | edit_post
    resource_type = Column(String(50),  nullable=True)           # blog_post | pipeline_run | audit
    resource_id   = Column(Integer,     nullable=True)
    status        = Column(String(20),  default="success")       # success | error
    error_message = Column(Text,        nullable=True)
    ip_address    = Column(String(45),  nullable=True)
    extra         = Column(JSON,        nullable=True)           # arbitrary extra metadata
    created_at    = Column(DateTime,    default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_ual_user_action", "user_id", "action"),
        Index("ix_ual_shop_created", "shop_domain", "created_at"),
    )
