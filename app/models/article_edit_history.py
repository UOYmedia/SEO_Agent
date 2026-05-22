from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.database import Base


class ArticleEditHistory(Base):
    """User-initiated edits on a BlogPost. AI rewrites are not snapshotted."""
    __tablename__ = "article_edit_history"

    id          = Column(Integer, primary_key=True, index=True)
    post_id     = Column(Integer, ForeignKey("blog_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    shop_domain = Column(String(255), nullable=True, index=True)

    # Meta diffs — store before/after only when changed
    title_before           = Column(Text, nullable=True)
    title_after            = Column(Text, nullable=True)
    seo_title_before       = Column(Text, nullable=True)
    seo_title_after        = Column(Text, nullable=True)
    seo_description_before = Column(Text, nullable=True)
    seo_description_after  = Column(Text, nullable=True)
    tags_before            = Column(JSON, nullable=True)
    tags_after             = Column(JSON, nullable=True)

    # Content diff — unified diff string, only set when content_html actually changed
    content_diff = Column(Text, nullable=True)
    lines_added   = Column(Integer, default=0)
    lines_removed = Column(Integer, default=0)

    # Short human summary (e.g. "title; meta; content +12/-3")
    summary = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
