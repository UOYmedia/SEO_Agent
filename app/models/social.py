from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index,
    Integer, JSON, String, Text, UniqueConstraint,
)
from app.database import Base

SUPPORTED_PLATFORMS = ["twitter", "facebook", "pinterest", "threads", "linkedin", "tiktok", "youtube"]

PLATFORM_META = {
    "twitter":   {"name": "Twitter / X",  "icon": "𝕏",  "color": "#000000"},
    "facebook":  {"name": "Facebook",     "icon": "f",  "color": "#1877F2"},
    "pinterest": {"name": "Pinterest",    "icon": "P",  "color": "#E60023"},
    "threads":   {"name": "Threads",      "icon": "@",  "color": "#101010"},
    "linkedin":  {"name": "LinkedIn",     "icon": "in", "color": "#0A66C2"},
    "tiktok":    {"name": "TikTok",       "icon": "♪",  "color": "#010101"},
    "youtube":   {"name": "YouTube",      "icon": "▶",  "color": "#FF0000"},
}


class SocialAccount(Base):
    """OAuth credentials + profile info for one social platform per brand."""
    __tablename__ = "social_accounts"

    id                = Column(Integer, primary_key=True, index=True)
    shop_domain       = Column(String(255), nullable=False, index=True)
    platform          = Column(String(50),  nullable=False)

    # Cached profile snapshot
    platform_user_id  = Column(String(255), nullable=True)
    platform_username = Column(String(255), nullable=True)
    platform_avatar   = Column(String(512),  nullable=True)

    # OAuth tokens
    access_token      = Column(Text, nullable=True)
    refresh_token     = Column(Text, nullable=True)
    token_expires_at  = Column(DateTime, nullable=True)

    # Platform-specific: page_id + page_token (FB), board_id (Pinterest), channel_id (YT)…
    extra_config      = Column(JSON, nullable=True)

    is_active         = Column(Boolean, default=True)
    connected_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("shop_domain", "platform"),)


class SocialPost(Base):
    """Record of every post pushed to a social platform from a blog article."""
    __tablename__ = "social_posts"

    id               = Column(Integer, primary_key=True, index=True)
    blog_post_id     = Column(Integer, ForeignKey("blog_posts.id", ondelete="SET NULL"), nullable=True)
    shop_domain      = Column(String(255), nullable=False)
    platform         = Column(String(50),  nullable=False)

    platform_post_id  = Column(String(255), nullable=True)
    platform_post_url = Column(String(512),  nullable=True)
    content_used      = Column(Text, nullable=True)
    image_url         = Column(String(512),  nullable=True)

    # pending | published | failed | scheduled
    status           = Column(String(20), default="pending")
    scheduled_at     = Column(DateTime, nullable=True)
    published_at     = Column(DateTime, nullable=True)
    error_message    = Column(Text, nullable=True)
    engagement       = Column(JSON, nullable=True)   # {likes, shares, comments, clicks}

    created_at       = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_sp_shop_platform",  "shop_domain", "platform"),
        Index("ix_sp_blog_post",      "blog_post_id"),
        Index("ix_sp_shop_created",   "shop_domain", "created_at"),
    )
