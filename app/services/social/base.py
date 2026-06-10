"""Shared OAuth state store + helpers used by all platform publishers."""
import hashlib, secrets, time, base64
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.social import SocialAccount

# ── In-memory OAuth state store (platform-agnostic CSRF guard) ────────────────
# Maps random state string → {platform, shop_domain, code_verifier, expires}
_oauth_states: dict[str, dict] = {}
_STATE_TTL = 600  # 10 minutes


def generate_oauth_state(platform: str, shop_domain: str, code_verifier: str = "") -> str:
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "platform": platform,
        "shop_domain": shop_domain,
        "code_verifier": code_verifier,
        "expires": time.time() + _STATE_TTL,
    }
    return state


def consume_oauth_state(state: str) -> Optional[dict]:
    """Pop and return the state entry; None if missing or expired."""
    entry = _oauth_states.pop(state, None)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        return None
    return entry


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for OAuth 2.0 PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_account(db: Session, shop_domain: str, platform: str) -> Optional[SocialAccount]:
    return (
        db.query(SocialAccount)
        .filter_by(shop_domain=shop_domain, platform=platform, is_active=True)
        .first()
    )


def upsert_account(db: Session, shop_domain: str, platform: str, **kwargs) -> SocialAccount:
    account = (
        db.query(SocialAccount).filter_by(shop_domain=shop_domain, platform=platform).first()
    )
    if account:
        for k, v in kwargs.items():
            setattr(account, k, v)
        account.updated_at = datetime.utcnow()
    else:
        account = SocialAccount(shop_domain=shop_domain, platform=platform, **kwargs)
        db.add(account)
    db.commit()
    db.refresh(account)
    return account


def is_token_expired(account: SocialAccount) -> bool:
    if not account.token_expires_at:
        return False
    return datetime.utcnow() >= account.token_expires_at
