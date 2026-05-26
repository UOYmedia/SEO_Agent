from datetime import datetime, date as date_type

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class KeywordFollow(Base):
    __tablename__ = "keyword_follows"

    id          = Column(Integer, primary_key=True, index=True)
    shop_domain = Column(String(255), nullable=False, index=True)
    keyword     = Column(String(500), nullable=False, index=True)
    source      = Column(String(50), default="manual")  # gsc | research | manual
    is_active   = Column(Boolean, default=True, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    history = relationship(
        "KeywordHistory", back_populates="follow",
        cascade="all, delete-orphan", order_by="KeywordHistory.date.desc()",
    )


class KeywordHistory(Base):
    __tablename__ = "keyword_history"

    id          = Column(Integer, primary_key=True, index=True)
    follow_id   = Column(Integer, ForeignKey("keyword_follows.id"), nullable=False, index=True)
    date        = Column(Date, nullable=False, index=True)
    position    = Column(Float)      # GSC avg position (1 = top)
    clicks      = Column(Integer)    # GSC clicks
    impressions = Column(Integer)    # GSC impressions
    ctr         = Column(Float)      # GSC click-through rate (0-1)
    volume      = Column(Integer)    # DataForSEO monthly search volume
    cpc         = Column(Float)      # DataForSEO CPC
    created_at  = Column(DateTime, default=datetime.utcnow)

    follow = relationship("KeywordFollow", back_populates="history")
