"""TikTok publisher — Open Platform API v2 with PKCE."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state, pkce_pair

logger = logging.getLogger(__name__)

_AUTH_URL  = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_API_BASE  = "https://open.tiktokapis.com/v2"
_SCOPES    = "user.info.basic,video.publish,video.upload"


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/tiktok/callback"


def get_auth_url(shop_domain: str) -> str:
    verifier, challenge = pkce_pair()
    state = generate_oauth_state("tiktok", shop_domain, code_verifier=verifier)
    return f"{_AUTH_URL}?" + urlencode({
        "client_key":              settings.TIKTOK_CLIENT_KEY,
        "redirect_uri":            _redirect_uri(),
        "response_type":           "code",
        "scope":                   _SCOPES,
        "state":                   state,
        "code_challenge":          challenge,
        "code_challenge_method":   "S256",
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(_TOKEN_URL, data={
            "client_key":     settings.TIKTOK_CLIENT_KEY,
            "client_secret":  settings.TIKTOK_CLIENT_SECRET,
            "code":           code,
            "grant_type":     "authorization_code",
            "redirect_uri":   _redirect_uri(),
            "code_verifier":  entry["code_verifier"],
        })
        token_resp.raise_for_status()
        tokens = token_resp.json().get("data", token_resp.json())

        me_resp = await client.get(
            f"{_API_BASE}/user/info/",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            params={"fields": "open_id,union_id,avatar_url,display_name,username"},
        )
        me = me_resp.json().get("data", {}).get("user", {}) if me_resp.is_success else {}

    expires_in = tokens.get("expires_in", 86400)
    return {
        "access_token":      tokens["access_token"],
        "refresh_token":     tokens.get("refresh_token"),
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        "platform_user_id":  me.get("open_id") or me.get("union_id"),
        "platform_username": me.get("display_name") or me.get("username"),
        "platform_avatar":   me.get("avatar_url"),
        "shop_domain":       entry["shop_domain"],
    }


async def refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_TOKEN_URL, data={
            "client_key":    settings.TIKTOK_CLIENT_KEY,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        })
        resp.raise_for_status()
        tokens = resp.json().get("data", resp.json())
    expires_in = tokens.get("expires_in", 86400)
    return {
        "access_token":     tokens["access_token"],
        "refresh_token":    tokens.get("refresh_token", refresh_token),
        "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
    }


async def post_photo(access_token: str, text: str, image_url: str) -> dict:
    """
    TikTok photo/carousel post via Direct Post API.
    Requires the image to be a publicly accessible URL.
    Note: Text-only posts are not supported via the current Open Platform API.
    """
    payload = {
        "post_info": {
            "title":         text[:150],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet":  False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source":     "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": [image_url],
        },
        "post_mode":   "DIRECT_POST",
        "media_type":  "PHOTO",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_API_BASE}/post/publish/content/init/",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

    pub_id = data.get("publish_id", "")
    return {
        "platform_post_id":  pub_id,
        "platform_post_url": "https://www.tiktok.com/",  # TikTok API doesn't return post URL
    }
