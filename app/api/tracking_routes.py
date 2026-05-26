"""
Keyword Follow & Daily Tracking
- Follow keywords from GSC / research / manual
- Daily job collects position, clicks, volume per keyword
- View history by date
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.keyword_follow import KeywordFollow, KeywordHistory

tracking_router = APIRouter(prefix="/api/v1/tracking", tags=["tracking"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class FollowRequest(BaseModel):
    keyword: str
    shop_domain: str
    source: str = "manual"   # gsc | research | manual


class BulkFollowRequest(BaseModel):
    keywords: list[str]
    shop_domain: str
    source: str = "manual"


# ── Follow / Unfollow ─────────────────────────────────────────────────────────

@tracking_router.post("/follow")
def follow_keyword(body: FollowRequest, db: Session = Depends(get_db)):
    """Start following a keyword."""
    kw = body.keyword.strip().lower()
    if not kw:
        raise HTTPException(422, "keyword required")
    existing = db.query(KeywordFollow).filter_by(
        shop_domain=body.shop_domain, keyword=kw
    ).first()
    if existing:
        existing.is_active = True   # re-activate if previously unfollowed
        db.commit()
        return _follow_out(existing)
    follow = KeywordFollow(shop_domain=body.shop_domain, keyword=kw, source=body.source)
    db.add(follow)
    db.commit()
    db.refresh(follow)
    return _follow_out(follow)


@tracking_router.post("/follow/bulk")
def follow_keywords_bulk(body: BulkFollowRequest, db: Session = Depends(get_db)):
    """Follow multiple keywords at once."""
    created = []
    for raw in body.keywords:
        kw = raw.strip().lower()
        if not kw:
            continue
        existing = db.query(KeywordFollow).filter_by(
            shop_domain=body.shop_domain, keyword=kw
        ).first()
        if existing:
            existing.is_active = True
            created.append(_follow_out(existing))
        else:
            follow = KeywordFollow(shop_domain=body.shop_domain, keyword=kw, source=body.source)
            db.add(follow)
            db.flush()
            created.append(_follow_out(follow))
    db.commit()
    return {"followed": len(created), "items": created}


@tracking_router.delete("/follow/{follow_id}")
def unfollow_keyword(follow_id: int, db: Session = Depends(get_db)):
    """Stop following a keyword (soft-delete — keeps history)."""
    follow = db.query(KeywordFollow).filter_by(id=follow_id).first()
    if not follow:
        raise HTTPException(404, "Follow not found")
    follow.is_active = False
    db.commit()
    return {"unfollowed": follow_id, "keyword": follow.keyword}


# ── List & History ────────────────────────────────────────────────────────────

@tracking_router.get("/follows")
def list_follows(
    shop_domain: Optional[str] = Query(None),
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
):
    """List followed keywords with their latest snapshot."""
    q = db.query(KeywordFollow)
    if shop_domain:
        q = q.filter(KeywordFollow.shop_domain == shop_domain)
    if active_only:
        q = q.filter(KeywordFollow.is_active == True)
    follows = q.order_by(KeywordFollow.created_at.desc()).all()

    result = []
    for f in follows:
        latest = (
            db.query(KeywordHistory)
            .filter_by(follow_id=f.id)
            .order_by(KeywordHistory.date.desc())
            .first()
        )
        prev = (
            db.query(KeywordHistory)
            .filter_by(follow_id=f.id)
            .order_by(KeywordHistory.date.desc())
            .offset(1).first()
        )
        item = _follow_out(f)
        item["latest"] = _history_out(latest) if latest else None
        item["position_change"] = (
            round((prev.position or 0) - (latest.position or 0), 1)
            if latest and prev and latest.position and prev.position else None
        )
        result.append(item)
    return result


@tracking_router.get("/follows/{follow_id}/history")
def get_keyword_history(
    follow_id: int,
    days: int = Query(90, ge=7, le=365),
    db: Session = Depends(get_db),
):
    """Get daily history for a followed keyword."""
    follow = db.query(KeywordFollow).filter_by(id=follow_id).first()
    if not follow:
        raise HTTPException(404, "Follow not found")
    cutoff = date.today() - timedelta(days=days)
    rows = (
        db.query(KeywordHistory)
        .filter(KeywordHistory.follow_id == follow_id, KeywordHistory.date >= cutoff)
        .order_by(KeywordHistory.date.asc())
        .all()
    )
    return {
        "follow": _follow_out(follow),
        "history": [_history_out(r) for r in rows],
    }


# ── Manual collect trigger ────────────────────────────────────────────────────

@tracking_router.post("/collect")
async def trigger_collect(
    shop_domain: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Manually trigger daily data collection for all active follows."""
    count = await _collect_all(db, shop_domain)
    return {"collected": count, "date": date.today().isoformat()}


# ── Collection logic ──────────────────────────────────────────────────────────

async def _collect_all(db: Session, shop_domain: Optional[str] = None) -> int:
    """Collect today's data for all active follows. Returns count of snapshots saved."""
    import logging
    logger = logging.getLogger(__name__)

    q = db.query(KeywordFollow).filter(KeywordFollow.is_active == True)
    if shop_domain:
        q = q.filter(KeywordFollow.shop_domain == shop_domain)
    follows = q.all()
    if not follows:
        return 0

    today = date.today()
    count = 0

    # Group by shop_domain to reuse GSC client per shop
    shops: dict[str, list[KeywordFollow]] = {}
    for f in follows:
        shops.setdefault(f.shop_domain, []).append(f)

    for shop, shop_follows in shops.items():
        # Get GSC client for this shop
        gsc = None
        try:
            from app.services.gsc_client import get_client_for_brand
            gsc = get_client_for_brand(shop, db)
        except Exception as e:
            logger.warning("GSC client unavailable for %s: %s", shop, e)

        # Get search volumes in bulk
        from app.services.volume_service import get_search_volumes, is_configured
        keywords = [f.keyword for f in shop_follows]
        volumes: dict = {}
        if is_configured():
            try:
                volumes = await get_search_volumes(keywords)
            except Exception as e:
                logger.warning("Volume fetch failed for %s: %s", shop, e)

        for follow in shop_follows:
            # Skip if already collected today
            exists = db.query(KeywordHistory).filter_by(
                follow_id=follow.id, date=today
            ).first()
            if exists:
                continue

            snap = KeywordHistory(follow_id=follow.id, date=today)

            # GSC data
            if gsc:
                try:
                    rows = gsc.get_keyword_history(follow.keyword, days=2)
                    if rows:
                        r = rows[-1]  # most recent day
                        snap.clicks      = r.get("clicks")
                        snap.impressions = r.get("impressions")
                        snap.ctr         = r.get("ctr")
                        snap.position    = r.get("position")
                except Exception as e:
                    logger.warning("GSC fetch failed for '%s': %s", follow.keyword, e)

            # Volume data
            vol = volumes.get(follow.keyword, {})
            snap.volume = vol.get("volume")
            snap.cpc    = vol.get("cpc")

            db.add(snap)
            count += 1

    db.commit()
    return count


# ── Helpers ───────────────────────────────────────────────────────────────────

def _follow_out(f: KeywordFollow) -> dict:
    return {
        "id":          f.id,
        "keyword":     f.keyword,
        "shop_domain": f.shop_domain,
        "source":      f.source,
        "is_active":   f.is_active,
        "created_at":  f.created_at,
    }


def _history_out(h: KeywordHistory) -> dict:
    return {
        "id":          h.id,
        "date":        h.date.isoformat() if h.date else None,
        "position":    h.position,
        "clicks":      h.clicks,
        "impressions": h.impressions,
        "ctr":         h.ctr,
        "volume":      h.volume,
        "cpc":         h.cpc,
    }
