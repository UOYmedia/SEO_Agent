"""Background scheduler — auto-publishes posts when their scheduled_at time arrives."""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def _publish_due_posts():
    from app.database import SessionLocal
    from app.models.blog_post import BlogPost, PostStatus

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        due = (
            db.query(BlogPost)
            .filter(
                BlogPost.scheduled_at.isnot(None),
                BlogPost.scheduled_at <= now,
                BlogPost.status == PostStatus.DRAFT,
                BlogPost.shop_domain.isnot(None),
                BlogPost.scheduled_blog_id.isnot(None),
            )
            .all()
        )
        for post in due:
            try:
                from app.services.shopify_publisher import ShopifyPublisher
                pub = ShopifyPublisher(shop_domain=post.shop_domain, db=db)
                article = await pub.publish_article(
                    post=post,
                    blog_id=int(post.scheduled_blog_id),
                    author="SEO Agent",
                    published=True,
                    image_url=post.featured_image_url,
                    image_alt=post.featured_image_alt or post.title,
                )
                blog_handle = None
                try:
                    from app.models.blog_post import BlogChannel
                    ch = db.query(BlogChannel).filter_by(
                        platform_id=post.scheduled_blog_id,
                        shop_domain=post.shop_domain,
                    ).first()
                    if ch:
                        blog_handle = ch.handle
                except Exception:
                    pass
                pub.sync_after_publish(db, post, article, blog_handle)
                post.scheduled_at = None
                post.scheduled_blog_id = None
                db.commit()
                logger.info(f"Auto-published post {post.id}: {post.title}")
            except Exception as e:
                logger.error(f"Failed to auto-publish post {post.id}: {e}")
    finally:
        db.close()


async def _collect_keyword_snapshots():
    from app.database import SessionLocal
    from app.api.tracking_routes import _collect_all
    db = SessionLocal()
    try:
        count = await _collect_all(db)
        logger.info("Keyword tracking: collected %d snapshots", count)
    except Exception as e:
        logger.error("Keyword snapshot collection failed: %s", e)
    finally:
        db.close()


def start():
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_publish_due_posts, "interval", minutes=1, id="publish_scheduled")
    _scheduler.add_job(_collect_keyword_snapshots, "cron", hour=2, minute=0, id="keyword_tracking")
    _scheduler.start()
    logger.info("Scheduler started — publishing check every minute, keyword tracking daily at 02:00")


def stop():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
