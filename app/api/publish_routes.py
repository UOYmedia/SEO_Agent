"""
Publish drafts to Shopify. Image generation happens during article
creation or draft editing — NOT here.

Routes:
  POST /api/v1/publish/{post_id}/shopify   — publish immediately
  POST /api/v1/publish/{post_id}/schedule  — schedule for later
  DELETE /api/v1/publish/{post_id}/schedule — cancel schedule
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.blog_post import BlogChannel, BlogPost
from app.schemas.publish import PublishToShopifyRequest
from app.services.shopify_publisher import ShopifyPublisher

publish_router = APIRouter(prefix="/api/v1/publish", tags=["publish"])


def _get_post_or_404(post_id: int, db: Session) -> BlogPost:
    post = db.query(BlogPost).filter(BlogPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _channel_handle(db: Session, channel_id: Optional[int]) -> Optional[str]:
    if not channel_id:
        return None
    ch = db.query(BlogChannel).filter(BlogChannel.id == channel_id).first()
    return ch.handle if ch else None


@publish_router.post("/{post_id}/shopify")
async def publish_to_shopify(
    post_id: int,
    body: PublishToShopifyRequest,
    db: Session = Depends(get_db),
):
    """Publish a draft to Shopify. Uses the post's existing featured_image_url
    (generated at draft time). Does not call DALL-E."""
    post = _get_post_or_404(post_id, db)

    publisher = ShopifyPublisher(shop_domain=body.shop_domain, db=db)
    try:
        article = await publisher.publish_article(
            post=post,
            blog_id=body.blog_id,
            author=body.author,
            published=body.published,
            image_url=post.featured_image_url or None,
            image_alt=post.featured_image_alt or post.title,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Shopify API error: {e}")

    blog_handle = _channel_handle(db, post.channel_id)
    updated = publisher.sync_after_publish(db, post, article, blog_handle)

    return {
        "post_id": updated.id,
        "shopify_article_id": updated.platform_id,
        "platform_url": updated.platform_url,
        "featured_image_url": updated.featured_image_url,
        "status": updated.status,
        "image_uploaded": bool(post.featured_image_url),
    }


class _ScheduleBody(BaseModel):
    scheduled_at: datetime
    shop_domain: str
    blog_id: int


@publish_router.post("/{post_id}/schedule")
def schedule_post(
    post_id: int,
    body: _ScheduleBody,
    db: Session = Depends(get_db),
):
    """Schedule a draft post to auto-publish at a future datetime (UTC)."""
    post = _get_post_or_404(post_id, db)
    from app.models.blog_post import PostStatus
    if post.status != PostStatus.DRAFT:
        raise HTTPException(422, "Only draft posts can be scheduled")
    post.scheduled_at = body.scheduled_at
    post.scheduled_blog_id = str(body.blog_id)
    if body.shop_domain:
        post.shop_domain = body.shop_domain
    db.commit()
    return {
        "post_id": post.id,
        "scheduled_at": post.scheduled_at,
        "scheduled_blog_id": post.scheduled_blog_id,
        "shop_domain": post.shop_domain,
    }


@publish_router.delete("/{post_id}/schedule")
def cancel_schedule(post_id: int, db: Session = Depends(get_db)):
    """Cancel a scheduled post — it remains as a draft."""
    post = _get_post_or_404(post_id, db)
    post.scheduled_at = None
    post.scheduled_blog_id = None
    db.commit()
    return {"post_id": post.id, "scheduled_at": None}
