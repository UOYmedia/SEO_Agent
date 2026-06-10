"""LinkedIn publisher — OAuth 2.0 + UGC Posts API."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state

logger = logging.getLogger(__name__)

_AUTH_URL  = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_API_BASE  = "https://api.linkedin.com/v2"
_SCOPES    = "openid profile email w_member_social"


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/linkedin/callback"


def get_auth_url(shop_domain: str) -> str:
    state = generate_oauth_state("linkedin", shop_domain)
    return f"{_AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id":     settings.LINKEDIN_CLIENT_ID,
        "redirect_uri":  _redirect_uri(),
        "state":         state,
        "scope":         _SCOPES,
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  _redirect_uri(),
                "client_id":     settings.LINKEDIN_CLIENT_ID,
                "client_secret": settings.LINKEDIN_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        me_resp = await client.get(
            f"{_API_BASE}/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        me = me_resp.json() if me_resp.is_success else {}

    expires_in = tokens.get("expires_in", 5184000)
    return {
        "access_token":      tokens["access_token"],
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        # LinkedIn doesn't provide refresh tokens on standard tier
        "platform_user_id":  me.get("sub"),
        "platform_username": me.get("name"),
        "platform_avatar":   me.get("picture"),
        "shop_domain":       entry["shop_domain"],
    }


async def create_post(access_token: str, author_urn: str, text: str) -> dict:
    """Post via UGC Posts API (v2)."""
    payload = {
        "author":          author_urn,  # urn:li:person:{id}
        "lifecycleState":  "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary":   {"text": text[:3000]},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_API_BASE}/ugcPosts",
            json=payload,
            headers={
                "Authorization":            f"Bearer {access_token}",
                "Content-Type":             "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
        )
        resp.raise_for_status()
        post_id = resp.headers.get("x-restli-id", "")

    return {
        "platform_post_id":  post_id,
        "platform_post_url": f"https://www.linkedin.com/feed/update/{post_id}/",
    }
