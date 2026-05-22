from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, String
from app.database import Base


class ShopifyStore(Base):
    __tablename__ = "shopify_stores"

    id           = Column(Integer, primary_key=True)
    shop_domain  = Column(String(255), unique=True, nullable=False, index=True)
    access_token = Column(String(500), nullable=False)
    scope        = Column(String(500))
    installed_at = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
