"""Social media integration routes.

OAuth connect/disconnect, content publishing, post history.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.blog_post import BlogPost
from app.models.social import PLATFORM_META, SUPPORTED_PLATFORMS, SocialAccount, SocialPost
from app.services.social import base as svc_base
from app.services.social.formatter import format_for_platform
from app.config import settings

logger = logging.getLogger(__name__)

social_router = APIRouter(prefix="/api/v1/social", tags=["social"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _account_dict(a: SocialAccount) -> dict:
    return {
        "id":               a.id,
        "platform":         a.platform,
        "platform_meta":    PLATFORM_META.get(a.platform, {}),
        "platform_user_id": a.platform_user_id,
        "username":         a.platform_username,
        "avatar":           a.platform_avatar,
        "extra_config":     a.extra_config or {},
        "is_active":        a.is_active,
        "connected_at":     a.created_at.isoformat() if a.created_at else None,
    }


def _get_platform_module(platform: str):
    if platform == "twitter":
        from app.services.social import twitter as m
    elif platform == "facebook":
        from app.services.social import facebook as m
    elif platform == "pinterest":
        from app.services.social import pinterest as m
    elif platform == "threads":
        from app.services.social import threads as m
    elif platform == "linkedin":
        from app.services.social import linkedin as m
    elif platform == "tiktok":
        from app.services.social import tiktok as m
    elif platform == "youtube":
        from app.services.social import youtube as m
    else:
        raise HTTPException(404, f"Unknown platform: {platform}")
    return m


def _is_configured(platform: str) -> bool:
    """Check whether the platform's API credentials are set in config."""
    if platform in ("twitter",):
        return bool(settings.TWITTER_CLIENT_ID)
    if platform in ("facebook", "threads"):
        return bool(settings.FACEBOOK_APP_ID)
    if platform == "pinterest":
        return bool(settings.PINTEREST_APP_ID)
    if platform == "linkedin":
        return bool(settings.LINKEDIN_CLIENT_ID)
    if platform == "tiktok":
        return bool(settings.TIKTOK_CLIENT_KEY)
    if platform == "youtube":
        return bool(settings.GOOGLE_CLIENT_ID)
    return False


async def _ensure_fresh_token(account: SocialAccount, db: Session) -> SocialAccount:
    """Refresh access token if it's expired (for platforms that support it)."""
    if not svc_base.is_token_expired(account):
        return account
    try:
        m = _get_platform_module(account.platform)
        if not hasattr(m, "refresh_tokens"):
            return account
        new_tokens = await m.refresh_tokens(account.refresh_token or account.access_token)
        for k, v in new_tokens.items():
            setattr(account, k, v)
        account.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(account)
    except Exception as exc:
        logger.warning("Token refresh failed (%s): %s", account.platform, exc)
    return account


# ── Platform list ─────────────────────────────────────────────────────────────

@social_router.get("/platforms")
def list_platforms():
    """Return metadata + configured status for every supported platform."""
    return [
        {
            "platform":    p,
            **PLATFORM_META[p],
            "configured":  _is_configured(p),
            "needs_image": p in ("tiktok",),
            "video_only":  False,
        }
        for p in SUPPORTED_PLATFORMS
    ]


# ── Connected accounts ────────────────────────────────────────────────────────

@social_router.get("/accounts")
def list_accounts(shop_domain: str = Query(...), db: Session = Depends(get_db)):
    accounts = db.query(SocialAccount).filter_by(shop_domain=shop_domain, is_active=True).all()
    return [_account_dict(a) for a in accounts]


@social_router.delete("/accounts/{platform}")
def disconnect_account(
    platform: str,
    shop_domain: str = Query(...),
    db: Session = Depends(get_db),
):
    account = db.query(SocialAccount).filter_by(shop_domain=shop_domain, platform=platform).first()
    if not account:
        raise HTTPException(404, "Account not found")
    account.is_active = False
    account.access_token = None
    account.refresh_token = None
    db.commit()
    return {"ok": True}


class ManualTokenBody(BaseModel):
    shop_domain:       str
    platform:          str
    access_token:      str
    refresh_token:     Optional[str] = None
    platform_user_id:  Optional[str] = None
    platform_username: Optional[str] = None
    extra_config:      Optional[dict] = None


@social_router.post("/accounts/manual")
def save_manual_token(body: ManualTokenBody, db: Session = Depends(get_db)):
    """Allow pasting a token manually (dev/testing or platforms without full OAuth support)."""
    account = svc_base.upsert_account(
        db,
        shop_domain=body.shop_domain,
        platform=body.platform,
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        platform_user_id=body.platform_user_id,
        platform_username=body.platform_username,
        extra_config=body.extra_config or {},
        is_active=True,
    )
    return _account_dict(account)


class SelectConfigBody(BaseModel):
    shop_domain:  str
    platform:     str
    extra_config: dict   # page_id/page_token for FB, board_id for Pinterest, etc.


@social_router.post("/accounts/select-config")
def select_config(body: SelectConfigBody, db: Session = Depends(get_db)):
    """Store page/board selection after OAuth for FB and Pinterest."""
    account = db.query(SocialAccount).filter_by(
        shop_domain=body.shop_domain, platform=body.platform
    ).first()
    if not account:
        raise HTTPException(404, "Account not connected")
    account.extra_config = {**(account.extra_config or {}), **body.extra_config}
    account.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return _account_dict(account)


# ── OAuth start ───────────────────────────────────────────────────────────────

@social_router.get("/oauth/{platform}/start")
def oauth_start(platform: str, shop_domain: str = Query(...)):
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(404, f"Unknown platform: {platform}")
    if not _is_configured(platform):
        raise HTTPException(400, f"{platform} API credentials not configured")
    m = _get_platform_module(platform)
    url = m.get_auth_url(shop_domain)
    return RedirectResponse(url)


# ── OAuth callbacks (one per platform) ────────────────────────────────────────

def _frontend_redirect(platform: str, status: str, extra: str = "") -> RedirectResponse:
    base = settings.APP_URL or ""
    url = f"{base}/?page=social&oauth_platform={platform}&oauth_status={status}"
    if extra:
        url += f"&{extra}"
    return RedirectResponse(url)


@social_router.get("/oauth/twitter/callback")
async def twitter_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("twitter", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import twitter as m
        data = await m.exchange_code(code, state)
        svc_base.upsert_account(db, data["shop_domain"], "twitter",
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            is_active=True,
        )
        return _frontend_redirect("twitter", "connected")
    except Exception as exc:
        logger.error("Twitter OAuth callback error: %s", exc)
        return _frontend_redirect("twitter", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/facebook/callback")
async def facebook_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("facebook", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import facebook as m
        data = await m.exchange_code(code, state)
        pages = data.pop("pages", [])
        account = svc_base.upsert_account(db, data["shop_domain"], "facebook",
            access_token=data["access_token"],
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            extra_config={"pages": pages},
            is_active=True,
        )
        # If user manages exactly one page, auto-select it
        if len(pages) == 1:
            p = pages[0]
            account.extra_config = {
                "page_id":    p["id"],
                "page_name":  p["name"],
                "page_token": p["access_token"],
                "pages":      pages,
            }
            account.updated_at = datetime.utcnow()
            db.commit()
            return _frontend_redirect("facebook", "connected")
        return _frontend_redirect("facebook", "select_page", f"account_id={account.id}")
    except Exception as exc:
        logger.error("Facebook OAuth callback error: %s", exc)
        return _frontend_redirect("facebook", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/pinterest/callback")
async def pinterest_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("pinterest", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import pinterest as m
        data = await m.exchange_code(code, state)
        boards = data.pop("boards", [])
        account = svc_base.upsert_account(db, data["shop_domain"], "pinterest",
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            extra_config={"boards": boards},
            is_active=True,
        )
        if len(boards) == 1:
            b = boards[0]
            account.extra_config = {
                "board_id":   b["id"],
                "board_name": b.get("name"),
                "boards":     boards,
            }
            account.updated_at = datetime.utcnow()
            db.commit()
            return _frontend_redirect("pinterest", "connected")
        return _frontend_redirect("pinterest", "select_board", f"account_id={account.id}")
    except Exception as exc:
        logger.error("Pinterest OAuth callback error: %s", exc)
        return _frontend_redirect("pinterest", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/threads/callback")
async def threads_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("threads", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import threads as m
        data = await m.exchange_code(code, state)
        svc_base.upsert_account(db, data["shop_domain"], "threads",
            access_token=data["access_token"],
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            is_active=True,
        )
        return _frontend_redirect("threads", "connected")
    except Exception as exc:
        logger.error("Threads OAuth callback error: %s", exc)
        return _frontend_redirect("threads", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/linkedin/callback")
async def linkedin_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("linkedin", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import linkedin as m
        data = await m.exchange_code(code, state)
        svc_base.upsert_account(db, data["shop_domain"], "linkedin",
            access_token=data["access_token"],
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            extra_config={"author_urn": f"urn:li:person:{data.get('platform_user_id')}"},
            is_active=True,
        )
        return _frontend_redirect("linkedin", "connected")
    except Exception as exc:
        logger.error("LinkedIn OAuth callback error: %s", exc)
        return _frontend_redirect("linkedin", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/tiktok/callback")
async def tiktok_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("tiktok", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import tiktok as m
        data = await m.exchange_code(code, state)
        svc_base.upsert_account(db, data["shop_domain"], "tiktok",
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            is_active=True,
        )
        return _frontend_redirect("tiktok", "connected")
    except Exception as exc:
        logger.error("TikTok OAuth callback error: %s", exc)
        return _frontend_redirect("tiktok", "error", f"error={str(exc)[:100]}")


@social_router.get("/oauth/youtube/callback")
async def youtube_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error or not code:
        return _frontend_redirect("youtube", "error", f"error={error or 'no_code'}")
    try:
        from app.services.social import youtube as m
        data = await m.exchange_code(code, state)
        svc_base.upsert_account(db, data["shop_domain"], "youtube",
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            token_expires_at=data.get("token_expires_at"),
            platform_user_id=data.get("platform_user_id"),
            platform_username=data.get("platform_username"),
            platform_avatar=data.get("platform_avatar"),
            extra_config=data.get("extra_config") or {},
            is_active=True,
        )
        return _frontend_redirect("youtube", "connected")
    except Exception as exc:
        logger.error("YouTube OAuth callback error: %s", exc)
        return _frontend_redirect("youtube", "error", f"error={str(exc)[:100]}")


# ── Content preview ────────────────────────────────────────────────────────────

class PreviewBody(BaseModel):
    blog_post_id: int
    platform:     str


@social_router.post("/preview")
async def preview_post(body: PreviewBody, db: Session = Depends(get_db)):
    post = db.query(BlogPost).filter_by(id=body.blog_post_id).first()
    if not post:
        raise HTTPException(404, "Blog post not found")
    article_url = post.platform_url or f"https://{post.shop_domain}/blogs/news/{post.slug or post.id}"
    result = await format_for_platform(
        platform=body.platform,
        title=post.title,
        content_html=post.content_html or "",
        article_url=article_url,
        keywords=([post.focus_keyword] if post.focus_keyword else []) + (post.semantic_keywords or []),
        image_url=post.featured_image_url,
    )
    return result


# ── Publishing ─────────────────────────────────────────────────────────────────

class PublishBody(BaseModel):
    blog_post_id: int
    shop_domain:  str
    platforms:    list[str]


@social_router.post("/publish")
async def publish_to_social(body: PublishBody, db: Session = Depends(get_db)):
    post = db.query(BlogPost).filter_by(id=body.blog_post_id).first()
    if not post:
        raise HTTPException(404, "Blog post not found")

    article_url = post.platform_url or f"https://{post.shop_domain}/blogs/news/{post.slug or post.id}"
    keywords = ([post.focus_keyword] if post.focus_keyword else []) + (post.semantic_keywords or [])

    results = []
    for platform in body.platforms:
        if platform not in SUPPORTED_PLATFORMS:
            results.append({"platform": platform, "status": "error", "error": "Unknown platform"})
            continue

        account = svc_base.get_account(db, body.shop_domain, platform)
        if not account:
            results.append({"platform": platform, "status": "error", "error": "Not connected"})
            continue

        # Create pending SocialPost record
        sp = SocialPost(
            blog_post_id=body.blog_post_id,
            shop_domain=body.shop_domain,
            platform=platform,
            status="pending",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)

        try:
            account = await _ensure_fresh_token(account, db)

            # Format content
            formatted = await format_for_platform(
                platform=platform,
                title=post.title,
                content_html=post.content_html or "",
                article_url=article_url,
                keywords=keywords,
                image_url=post.featured_image_url,
            )
            sp.content_used = formatted["text"]
            sp.image_url = formatted["image_url"]

            # Dispatch to platform
            pub_result = await _publish_to_platform(account, platform, formatted, article_url, post)

            sp.platform_post_id  = pub_result.get("platform_post_id")
            sp.platform_post_url = pub_result.get("platform_post_url")
            sp.status      = "published"
            sp.published_at = datetime.utcnow()
            db.commit()

            results.append({
                "platform":          platform,
                "status":            "published",
                "platform_post_url": sp.platform_post_url,
                "social_post_id":    sp.id,
            })

        except Exception as exc:
            logger.error("Social publish error (%s post %s): %s", platform, body.blog_post_id, exc)
            sp.status = "failed"
            sp.error_message = str(exc)[:500]
            db.commit()
            results.append({"platform": platform, "status": "error", "error": str(exc)[:200]})

    return {"results": results}


async def _publish_to_platform(
    account: SocialAccount,
    platform: str,
    formatted: dict,
    article_url: str,
    post: BlogPost,
) -> dict:
    text      = formatted["text"]
    image_url = formatted.get("image_url")
    cfg       = account.extra_config or {}

    if platform == "twitter":
        from app.services.social.twitter import post_tweet
        return await post_tweet(account.access_token, text)

    if platform == "facebook":
        page_id    = cfg.get("page_id")
        page_token = cfg.get("page_token")
        if not page_id or not page_token:
            raise ValueError("Facebook page not selected. Go to Settings → Social to select a page.")
        from app.services.social.facebook import post_to_page
        return await post_to_page(page_token, page_id, text, link=article_url, image_url=image_url)

    if platform == "pinterest":
        board_id = cfg.get("board_id")
        if not board_id:
            raise ValueError("Pinterest board not selected. Go to Settings → Social to select a board.")
        from app.services.social.pinterest import create_pin
        return await create_pin(account.access_token, board_id, post.title, text, article_url, image_url)

    if platform == "threads":
        user_id = account.platform_user_id
        if not user_id:
            raise ValueError("Threads user ID missing — reconnect the account.")
        from app.services.social.threads import create_post
        return await create_post(account.access_token, user_id, text, image_url)

    if platform == "linkedin":
        author_urn = cfg.get("author_urn") or f"urn:li:person:{account.platform_user_id}"
        from app.services.social.linkedin import create_post
        return await create_post(account.access_token, author_urn, text)

    if platform == "tiktok":
        if not image_url:
            raise ValueError("TikTok requires an image. Make sure the article has a featured image.")
        from app.services.social.tiktok import post_photo
        return await post_photo(account.access_token, text, image_url)

    if platform == "youtube":
        from app.services.social.youtube import post_community
        return await post_community(account.access_token, text, image_url)

    raise ValueError(f"No publisher for platform: {platform}")


# ── Post history ───────────────────────────────────────────────────────────────

@social_router.get("/posts")
def list_social_posts(
    shop_domain:  str = Query(...),
    blog_post_id: Optional[int] = Query(None),
    platform:     Optional[str] = Query(None),
    limit:        int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(SocialPost).filter_by(shop_domain=shop_domain)
    if blog_post_id:
        q = q.filter_by(blog_post_id=blog_post_id)
    if platform:
        q = q.filter_by(platform=platform)
    posts = q.order_by(SocialPost.created_at.desc()).limit(limit).all()
    return [
        {
            "id":               p.id,
            "blog_post_id":     p.blog_post_id,
            "platform":         p.platform,
            "platform_meta":    PLATFORM_META.get(p.platform, {}),
            "platform_post_id": p.platform_post_id,
            "platform_post_url": p.platform_post_url,
            "content_used":     p.content_used,
            "image_url":        p.image_url,
            "status":           p.status,
            "published_at":     p.published_at.isoformat() if p.published_at else None,
            "error_message":    p.error_message,
            "engagement":       p.engagement,
            "created_at":       p.created_at.isoformat() if p.created_at else None,
        }
        for p in posts
    ]


@social_router.post("/posts/{social_post_id}/retry")
async def retry_social_post(social_post_id: int, db: Session = Depends(get_db)):
    sp = db.query(SocialPost).filter_by(id=social_post_id).first()
    if not sp:
        raise HTTPException(404, "Social post not found")
    if sp.status == "published":
        raise HTTPException(400, "Already published")

    blog_post = db.query(BlogPost).filter_by(id=sp.blog_post_id).first()
    if not blog_post:
        raise HTTPException(404, "Blog post not found")

    account = svc_base.get_account(db, sp.shop_domain, sp.platform)
    if not account:
        raise HTTPException(400, f"{sp.platform} account not connected")

    article_url = blog_post.platform_url or f"https://{sp.shop_domain}/blogs/news/{blog_post.slug or blog_post.id}"
    keywords = ([blog_post.focus_keyword] if blog_post.focus_keyword else []) + (blog_post.semantic_keywords or [])

    try:
        account = await _ensure_fresh_token(account, db)
        formatted = await format_for_platform(
            platform=sp.platform,
            title=blog_post.title,
            content_html=blog_post.content_html or "",
            article_url=article_url,
            keywords=keywords,
            image_url=blog_post.featured_image_url,
        )
        sp.content_used = formatted["text"]
        pub_result = await _publish_to_platform(account, sp.platform, formatted, article_url, blog_post)
        sp.platform_post_id  = pub_result.get("platform_post_id")
        sp.platform_post_url = pub_result.get("platform_post_url")
        sp.status      = "published"
        sp.published_at = datetime.utcnow()
        sp.error_message = None
        db.commit()
        return {"status": "published", "platform_post_url": sp.platform_post_url}
    except Exception as exc:
        sp.status = "failed"
        sp.error_message = str(exc)[:500]
        db.commit()
        raise HTTPException(500, str(exc))
