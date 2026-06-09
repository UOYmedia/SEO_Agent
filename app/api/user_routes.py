"""
User management + auth endpoints.

Bootstrap: first POST /api/v1/users/register creates an admin (no auth required).
Subsequent registrations require admin token.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User, UserStorePermission
from app.services.auth_service import (
    check_store_scope,
    create_access_token,
    get_current_user,
    get_user_shops,
    hash_password,
    require_admin,
    verify_password,
)

logger = logging.getLogger(__name__)


def _log_activity(
    db: Session,
    *,
    user_id: Optional[int],
    action: str,
    shop_domain: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    status: str = "success",
    error_message: Optional[str] = None,
    ip_address: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Insert a UserActivityLog row. Silently no-ops on any DB error."""
    try:
        from app.models.user_activity import UserActivityLog
        db.add(UserActivityLog(
            user_id=user_id,
            shop_domain=shop_domain,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            error_message=error_message,
            ip_address=ip_address,
            extra=extra,
        ))
        db.commit()
    except Exception as exc:
        logger.warning("_log_activity failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass

user_router = APIRouter(prefix="/api/v1/users", tags=["users"])


# ── Public: setup status (no auth required) ───────────────────────────────────

@user_router.get("/setup-status")
def setup_status(db: Session = Depends(get_db)):
    """Public endpoint — tells the UI whether to show Login or Register."""
    count = db.query(User).count()
    return {"bootstrapped": count > 0, "user_count": count}


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    email: str
    name: str
    password: str

class LoginBody(BaseModel):
    email: str
    password: str

class CreateUserBody(BaseModel):
    email: str
    name: str
    password: str
    role: str = "member"

class UpdateUserBody(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None

class GrantPermBody(BaseModel):
    shop_domain: str
    scopes: List[str] = ["read", "audit"]   # read | write | publish | audit


# ── Auth ──────────────────────────────────────────────────────────────────────

@user_router.post("/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    """
    First call (no users in DB) → creates admin.
    Subsequent calls require an admin Bearer token.
    """
    existing_count = db.query(User).count()
    is_bootstrap = existing_count == 0

    if not is_bootstrap:
        # Require admin auth for subsequent registrations
        from fastapi import Header
        raise HTTPException(403, "Use POST /api/v1/users/ (admin) to create more users")

    if db.query(User).filter_by(email=body.email).first():
        raise HTTPException(400, "Email already registered")

    user = User(
        email=body.email,
        name=body.name,
        hashed_password=hash_password(body.password),
        role="admin",   # first user is always admin
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.email, user.role)
    return {"token": token, "user": _user_out(user)}


@user_router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")

    # Update login tracking
    try:
        user.last_login_at = datetime.utcnow()
        user.login_count = (user.login_count or 0) + 1
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    _log_activity(
        db,
        user_id=user.id,
        action="login",
        ip_address=request.client.host if request.client else None,
    )

    token = create_access_token(user.id, user.email, user.role)
    return {"token": token, "user": _user_out(user, db)}


@user_router.get("/me")
def get_me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    return _user_out(user, db)


# ── User CRUD (admin) ─────────────────────────────────────────────────────────

@user_router.get("/")
def list_users(admin=Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [_user_out(u, db) for u in users]


@user_router.post("/")
def create_user(body: CreateUserBody, admin=Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(User).filter_by(email=body.email).first():
        raise HTTPException(400, "Email already registered")
    user = User(
        email=body.email,
        name=body.name,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@user_router.put("/{user_id}")
def update_user(user_id: int, body: UpdateUserBody, admin=Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if body.name is not None:
        user.name = body.name
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        user.hashed_password = hash_password(body.password)
    db.commit()
    return _user_out(user, db)


@user_router.patch("/{user_id}/kb-access")
def toggle_kb_access(user_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)):
    """Grant or revoke Knowledge Base access for a member user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.can_access_kb = not bool(user.can_access_kb)
    db.commit()
    return {"id": user.id, "can_access_kb": user.can_access_kb}


@user_router.delete("/{user_id}")
def delete_user(user_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.delete(user)
    db.commit()
    return {"deleted": user_id}


# ── Store Permissions ─────────────────────────────────────────────────────────

@user_router.get("/{user_id}/permissions")
def get_permissions(user_id: int, admin=Depends(require_admin), db: Session = Depends(get_db)):
    perms = db.query(UserStorePermission).filter_by(user_id=user_id).all()
    return [_perm_out(p) for p in perms]


@user_router.post("/{user_id}/permissions")
def grant_permission(
    user_id: int,
    body: GrantPermBody,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")

    valid_scopes = {"read", "write", "publish", "audit", "brand_edit"}
    bad = [s for s in body.scopes if s not in valid_scopes]
    if bad:
        raise HTTPException(422, f"Invalid scopes: {bad}. Valid: {sorted(valid_scopes)}")

    perm = db.query(UserStorePermission).filter_by(
        user_id=user_id, shop_domain=body.shop_domain
    ).first()
    if perm:
        perm.scopes = body.scopes
    else:
        perm = UserStorePermission(
            user_id=user_id,
            shop_domain=body.shop_domain,
            scopes=body.scopes,
            granted_by=admin.id,
        )
        db.add(perm)
    db.commit()
    return _perm_out(perm)


@user_router.delete("/{user_id}/permissions/{shop_domain:path}")
def revoke_permission(
    user_id: int,
    shop_domain: str,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    perm = db.query(UserStorePermission).filter_by(
        user_id=user_id, shop_domain=shop_domain
    ).first()
    if not perm:
        raise HTTPException(404, "Permission not found")
    db.delete(perm)
    db.commit()
    return {"revoked": shop_domain}


# ── Store list for current user ───────────────────────────────────────────────

@user_router.get("/stores/accessible")
def my_stores(user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Returns stores + scopes available to the current user."""
    if user.role == "admin":
        from app.models.shopify_store import ShopifyStore
        stores = db.query(ShopifyStore).all()
        return [
            {"shop_domain": s.shop_domain, "scopes": ["read","write","publish","audit"], "installed_at": s.installed_at}
            for s in stores
        ]
    perms = db.query(UserStorePermission).filter_by(user_id=user.id).all()
    return [_perm_out(p) for p in perms]


# ── Keyword suggestions for a store ──────────────────────────────────────────

@user_router.get("/stores/{shop_domain:path}/suggestions")
def store_keyword_suggestions(
    shop_domain: str,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """GPT-4o + Serper keyword suggestions scoped to the store's content."""
    check_store_scope(user, shop_domain, "audit", db)

    from app.models.blog_post import BlogPost, Platform
    from app.services.keyword_suggester import generate_suggestions

    store_posts = db.query(BlogPost).filter(
        BlogPost.platform == Platform.SHOPIFY,
        BlogPost.shop_domain == shop_domain,
    ).all()

    try:
        return generate_suggestions(store_posts, shop_domain)
    except Exception as e:
        raise HTTPException(502, f"Suggestion generation failed: {e}")


# ── Admin: Activity Report ───────────────────────────────────────────────────

@user_router.get("/admin/activity-report")
def activity_report(
    days: int = 30,
    admin=Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Comprehensive user activity report for the super-admin dashboard."""
    from app.models.blog_post import BlogPost
    from app.models.pipeline_run import PipelineRun
    from app.models.article_edit_history import ArticleEditHistory
    from app.models.article_feedback import ArticleFeedback
    from app.models.user_activity import UserActivityLog

    since = datetime.utcnow() - timedelta(days=days)
    since_7d = datetime.utcnow() - timedelta(days=7)

    # ── Summary ──────────────────────────────────────────────────────────────
    total_users   = db.query(func.count(User.id)).scalar() or 0
    active_7d     = db.query(func.count(User.id)).filter(
                        User.last_login_at >= since_7d).scalar() or 0
    total_pipelines  = db.query(func.count(PipelineRun.id)).filter(
                        PipelineRun.created_at >= since).scalar() or 0
    failed_pipelines = db.query(func.count(PipelineRun.id)).filter(
                        PipelineRun.created_at >= since,
                        PipelineRun.status == "failed").scalar() or 0
    total_articles   = db.query(func.count(BlogPost.id)).filter(
                        BlogPost.created_at >= since,
                        BlogPost.source == "generated").scalar() or 0

    # ── Per-user stats ────────────────────────────────────────────────────────
    users = db.query(User).order_by(User.last_login_at.desc().nullslast(), User.created_at.desc()).all()
    user_rows = []
    for u in users:
        # stores this user can access
        stores = (
            ["*all*"] if u.role == "admin"
            else [p.shop_domain for p in db.query(UserStorePermission).filter_by(user_id=u.id).all()]
        )

        # counts via shop_domain linkage
        pipeline_count, pipeline_errors = 0, 0
        article_count, published_count  = 0, 0
        for shop in (stores if stores != ["*all*"] else
                     [r[0] for r in db.query(PipelineRun.shop_domain).distinct()]):
            pipeline_count  += db.query(func.count(PipelineRun.id)).filter(
                PipelineRun.shop_domain == shop,
                PipelineRun.created_at  >= since).scalar() or 0
            pipeline_errors += db.query(func.count(PipelineRun.id)).filter(
                PipelineRun.shop_domain == shop,
                PipelineRun.status      == "failed",
                PipelineRun.created_at  >= since).scalar() or 0
            article_count   += db.query(func.count(BlogPost.id)).filter(
                BlogPost.shop_domain == shop,
                BlogPost.source      == "generated",
                BlogPost.created_at  >= since).scalar() or 0
            published_count += db.query(func.count(BlogPost.id)).filter(
                BlogPost.shop_domain == shop,
                BlogPost.source      == "generated",
                BlogPost.status      == "published",
                BlogPost.created_at  >= since).scalar() or 0

        edits_count    = db.query(func.count(ArticleEditHistory.id)).filter(
            ArticleEditHistory.user_id     == u.id,
            ArticleEditHistory.created_at  >= since).scalar() or 0
        feedback_count = db.query(func.count(ArticleFeedback.id)).filter(
            ArticleFeedback.user_id    == u.id,
            ArticleFeedback.created_at >= since).scalar() or 0
        login_count_period = db.query(func.count(UserActivityLog.id)).filter(
            UserActivityLog.user_id    == u.id,
            UserActivityLog.action     == "login",
            UserActivityLog.created_at >= since).scalar() or 0

        user_rows.append({
            "id":            u.id,
            "name":          u.name,
            "email":         u.email,
            "role":          u.role,
            "is_active":     u.is_active,
            "created_at":    u.created_at,
            "last_login_at": getattr(u, "last_login_at", None),
            "login_count_total": getattr(u, "login_count", 0) or 0,
            "login_count_period": login_count_period,
            "stores":        stores,
            "stats": {
                "pipeline_runs":    pipeline_count,
                "pipeline_errors":  pipeline_errors,
                "articles_generated": article_count,
                "articles_published": published_count,
                "edits_made":       edits_count,
                "feedback_given":   feedback_count,
            },
        })

    # ── Recent errors ─────────────────────────────────────────────────────────
    recent_errors = db.query(PipelineRun).filter(
        PipelineRun.status == "failed",
        PipelineRun.created_at >= since_7d,
    ).order_by(PipelineRun.created_at.desc()).limit(20).all()

    error_list = [{
        "type":       "pipeline_run",
        "id":         e.id,
        "keyword":    e.keyword,
        "shop_domain": e.shop_domain,
        "error":      e.error or "Unknown error",
        "created_at": e.created_at,
    } for e in recent_errors]

    # ── Recent activity feed (logins + pipeline events) ───────────────────────
    recent_logs = db.query(UserActivityLog).filter(
        UserActivityLog.created_at >= since_7d,
    ).order_by(UserActivityLog.created_at.desc()).limit(50).all()

    # Also include recent pipeline runs as activity
    recent_pipelines = db.query(PipelineRun).filter(
        PipelineRun.created_at >= since_7d,
    ).order_by(PipelineRun.created_at.desc()).limit(30).all()

    activity_feed = [
        {
            "type":        "login",
            "user_id":     log.user_id,
            "user_name":   next((u.name for u in users if u.id == log.user_id), "Unknown"),
            "shop_domain": log.shop_domain,
            "status":      log.status,
            "ip_address":  log.ip_address,
            "created_at":  log.created_at,
        }
        for log in recent_logs if log.action == "login"
    ] + [
        {
            "type":        "pipeline_run",
            "user_id":     None,
            "user_name":   None,
            "shop_domain": p.shop_domain,
            "keyword":     p.keyword,
            "status":      p.status,
            "created_at":  p.created_at,
        }
        for p in recent_pipelines
    ]
    activity_feed.sort(key=lambda x: x["created_at"] or datetime.min, reverse=True)

    return {
        "period_days": days,
        "generated_at": datetime.utcnow(),
        "summary": {
            "total_users":       total_users,
            "active_last_7_days": active_7d,
            "total_pipelines":   total_pipelines,
            "failed_pipelines":  failed_pipelines,
            "error_rate_pct":    round(failed_pipelines / total_pipelines * 100, 1) if total_pipelines else 0,
            "total_articles":    total_articles,
        },
        "users":          user_rows,
        "recent_errors":  error_list,
        "activity_feed":  activity_feed[:60],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user_out(user: User, db: Session = None) -> dict:
    perms = []
    if db:
        raw = db.query(UserStorePermission).filter_by(user_id=user.id).all()
        perms = [_perm_out(p) for p in raw]
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": user.is_active,
        "can_access_kb": user.role == "admin" or bool(getattr(user, "can_access_kb", False)),
        "created_at": user.created_at,
        "store_permissions": perms,
    }

def _perm_out(perm: UserStorePermission) -> dict:
    return {
        "shop_domain": perm.shop_domain,
        "scopes": perm.scopes or [],
        "created_at": perm.created_at,
    }
