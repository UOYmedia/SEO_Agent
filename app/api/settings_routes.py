"""
Brand profile / global settings endpoints.

GET  /api/v1/settings/brands                 — list all brand profiles
GET  /api/v1/settings/brand?shop_domain=...  — fetch single profile
PUT  /api/v1/settings/brand                  — create or update profile

Shopify OAuth credentials (DB override for env vars):
GET  /api/v1/settings/oauth                  — get current OAuth config (admin)
PUT  /api/v1/settings/oauth                  — save OAuth credentials to DB (admin)

GSC OAuth2 (per-brand):
GET  /api/v1/settings/gsc/connect?shop_domain=...  — start OAuth flow, returns auth URL
GET  /api/v1/settings/gsc/callback                 — OAuth2 callback, saves refresh_token
DELETE /api/v1/settings/gsc/disconnect?shop_domain=... — revoke & clear token
"""
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.brand_profile import BrandProfile
from app.services.auth_service import get_current_user

settings_router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_GSC_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GSC_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GSC_SCOPES = "https://www.googleapis.com/auth/webmasters.readonly"


class BrandProfileBody(BaseModel):
    shop_domain: Optional[str] = None
    brand_name: Optional[str] = None
    brand_style: Optional[str] = None
    brand_description: Optional[str] = None
    tone_of_voice: Optional[str] = None
    output_requirements: Optional[str] = None
    writing_notes: Optional[str] = None
    gsc_site_url: Optional[str] = None


def _profile_out(p: BrandProfile) -> dict:
    return {
        "shop_domain": p.shop_domain,
        "brand_name": p.brand_name or "",
        "brand_style": p.brand_style or "",
        "brand_description": p.brand_description or "",
        "tone_of_voice": p.tone_of_voice or "",
        "output_requirements": p.output_requirements or "",
        "writing_notes": p.writing_notes or "",
        "gsc_site_url": p.gsc_site_url or "",
        "gsc_connected": bool(p.gsc_refresh_token),
        "shared_user_ids": p.shared_user_ids or [],
        "updated_at": p.updated_at,
    }


def _can_view_brand(user, shop_domain: Optional[str], db: Session) -> bool:
    """User can view a brand profile if they have any store access (or are admin)."""
    if user.role == "admin":
        return True
    if shop_domain is None:
        return True  # global profile visible to all
    from app.models.user import UserStorePermission
    perm = db.query(UserStorePermission).filter_by(
        user_id=user.id, shop_domain=shop_domain
    ).first()
    return perm is not None


def _can_edit_brand(user, shop_domain: Optional[str], db: Session) -> bool:
    """User can edit a brand profile if they are admin or have brand_edit scope."""
    if user.role == "admin":
        return True
    if shop_domain is None:
        return False  # only admin can edit the global profile
    from app.models.user import UserStorePermission
    perm = db.query(UserStorePermission).filter_by(
        user_id=user.id, shop_domain=shop_domain
    ).first()
    return perm is not None and "brand_edit" in (perm.scopes or [])


@settings_router.get("/brands")
def list_brand_profiles(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admins see all brands. Non-admins see brands for their accessible stores."""
    if user.role == "admin":
        profiles = db.query(BrandProfile).order_by(BrandProfile.brand_name).all()
    else:
        from app.services.auth_service import get_user_shops
        user_shops = set(get_user_shops(user, db))
        all_profiles = db.query(BrandProfile).all()
        profiles = [
            p for p in all_profiles
            if p.shop_domain is None or (p.shop_domain and p.shop_domain in user_shops)
        ]
        profiles.sort(key=lambda p: p.brand_name or "")
    return [_profile_out(p) for p in profiles]


@settings_router.get("/brand")
def get_brand_profile(
    shop_domain: Optional[str] = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _can_view_brand(user, shop_domain, db):
        raise HTTPException(403, "No access to this store's brand profile")
    profile = db.query(BrandProfile).filter_by(shop_domain=shop_domain).first()
    if not profile:
        return {
            "shop_domain": shop_domain,
            "brand_name": "", "brand_style": "", "brand_description": "",
            "tone_of_voice": "", "output_requirements": "", "writing_notes": "",
            "gsc_site_url": "", "gsc_connected": False, "shared_user_ids": [],
            "can_edit": _can_edit_brand(user, shop_domain, db),
            "updated_at": None,
        }
    out = _profile_out(profile)
    out["can_edit"] = _can_edit_brand(user, shop_domain, db)
    return out


@settings_router.put("/brand")
def save_brand_profile(
    body: BrandProfileBody,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _can_edit_brand(user, body.shop_domain, db):
        raise HTTPException(403, "You don't have brand_edit permission for this store")
    profile = db.query(BrandProfile).filter_by(shop_domain=body.shop_domain).first()
    if not profile:
        profile = BrandProfile(shop_domain=body.shop_domain)
        db.add(profile)

    if body.brand_name is not None:
        profile.brand_name = body.brand_name
    if body.brand_style is not None:
        profile.brand_style = body.brand_style
    if body.brand_description is not None:
        profile.brand_description = body.brand_description
    if body.tone_of_voice is not None:
        profile.tone_of_voice = body.tone_of_voice
    if body.output_requirements is not None:
        profile.output_requirements = body.output_requirements
    if body.writing_notes is not None:
        profile.writing_notes = body.writing_notes
    if body.gsc_site_url is not None:
        profile.gsc_site_url = body.gsc_site_url or None

    db.commit()
    db.refresh(profile)
    return _profile_out(profile)


# ── Brand profile — who has edit access (admin view) ─────────────────────────

@settings_router.get("/brand/editors")
def list_brand_editors(
    shop_domain: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return users who have brand_edit scope on this store (admin only)."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from app.models.user import User, UserStorePermission
    if shop_domain:
        perms = (
            db.query(UserStorePermission)
            .filter(
                UserStorePermission.shop_domain == shop_domain,
                UserStorePermission.scopes.contains("brand_edit"),
            )
            .all()
        )
        user_ids = [p.user_id for p in perms]
    else:
        user_ids = []
    editors = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
    return [{"id": u.id, "name": u.name or u.email, "email": u.email} for u in editors]


# ── Shopify OAuth credentials ─────────────────────────────────────────────────

class OAuthSettingsBody(BaseModel):
    api_key: str = ""
    api_secret: str = ""   # send "" to leave unchanged, "***" is also ignored
    app_url: str = ""


@settings_router.get("/oauth")
def get_oauth_settings(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current Shopify Partner App credentials (DB override or env var)."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from app.models.system_settings import SystemSetting
    rows = {
        r.key: r.value
        for r in db.query(SystemSetting).filter(
            SystemSetting.key.in_(["SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "APP_URL"])
        ).all()
    }
    has_secret = bool(rows.get("SHOPIFY_API_SECRET") or settings.SHOPIFY_API_SECRET)
    return {
        "api_key":    rows.get("SHOPIFY_API_KEY") or settings.SHOPIFY_API_KEY or "",
        "api_secret": "***" if has_secret else "",   # never expose the actual secret
        "app_url":    rows.get("APP_URL") or settings.APP_URL or "",
        "source":     "db" if rows else "env",
    }


@settings_router.put("/oauth")
def save_oauth_settings(
    body: OAuthSettingsBody,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save Shopify Partner App credentials to DB (overrides env vars at runtime)."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from app.models.system_settings import SystemSetting

    def _upsert(key: str, value: str):
        if not value or value == "***":
            return
        row = db.query(SystemSetting).filter_by(key=key).first()
        if not row:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = value
        row.updated_at = datetime.utcnow()

    _upsert("SHOPIFY_API_KEY", body.api_key)
    _upsert("SHOPIFY_API_SECRET", body.api_secret)
    _upsert("APP_URL", body.app_url)
    db.commit()
    return {"saved": True}


# ── GSC OAuth2 ────────────────────────────────────────────────────────────────

@settings_router.get("/gsc/connect")
def gsc_connect(
    shop_domain: Optional[str] = Query(None),
    user=Depends(get_current_user),
):
    """Return the Google OAuth2 URL to start the GSC connection flow."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(422, "GOOGLE_CLIENT_ID not configured in Railway env vars")
    redirect_uri = f"{settings.APP_URL.rstrip('/')}/api/v1/settings/gsc/callback"
    params = {
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         _GSC_SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",        # force refresh_token every time
        "state":         shop_domain or "",
    }
    return {"auth_url": f"{_GSC_AUTH_URL}?{urlencode(params)}"}


@settings_router.get("/gsc/callback")
def gsc_callback(
    code: str = Query(...),
    state: str = Query(""),
    db: Session = Depends(get_db),
):
    """OAuth2 callback — exchange code for refresh_token and save to brand profile."""
    redirect_uri = f"{settings.APP_URL.rstrip('/')}/api/v1/settings/gsc/callback"
    resp = httpx.post(_GSC_TOKEN_URL, data={
        "code":          code,
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=15.0)
    if resp.status_code != 200:
        raise HTTPException(502, f"Google token exchange failed: {resp.text}")

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(422, "No refresh_token returned — make sure prompt=consent is set")

    shop_domain = state or None
    profile = db.query(BrandProfile).filter_by(shop_domain=shop_domain).first()
    if not profile:
        profile = BrandProfile(shop_domain=shop_domain)
        db.add(profile)
    profile.gsc_refresh_token = refresh_token
    db.commit()

    # Redirect back to app
    return RedirectResponse(url=f"{settings.APP_URL.rstrip('/')}/#brand", status_code=302)


@settings_router.delete("/gsc/disconnect")
def gsc_disconnect(
    shop_domain: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear the stored GSC refresh token for the brand."""
    profile = db.query(BrandProfile).filter_by(shop_domain=shop_domain).first()
    if profile:
        profile.gsc_refresh_token = None
        db.commit()
    return {"disconnected": True}
