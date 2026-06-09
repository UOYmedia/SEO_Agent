"""YouTube Community Post publisher — Google OAuth 2.0."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state

logger = logging.getLogger(__name__)

_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_YT_BASE   = "https://www.googleapis.com/youtube/v3"
_SCOPES    = (
    "https://www.googleapis.com/auth/youtube "
    "https://www.googleapis.com/auth/youtube.force-ssl"
)


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/youtube/callback"


def get_auth_url(shop_domain: str) -> str:
    state = generate_oauth_state("youtube", shop_domain)
    return f"{_AUTH_URL}?" + urlencode({
        "client_id":     settings.GOOGLE_CLIENT_ID,
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         _SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         state,
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(_TOKEN_URL, data={
            "client_id":     settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  _redirect_uri(),
        })
        token_resp.raise_for_status()
        tokens = token_resp.json()

        ch_resp = await client.get(
            f"{_YT_BASE}/channels",
            params={"part": "id,snippet,statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        channels = ch_resp.json().get("items", []) if ch_resp.is_success else []
        channel = channels[0] if channels else {}

    expires_in = tokens.get("expires_in", 3600)
    snippet = channel.get("snippet", {})
    return {
        "access_token":      tokens["access_token"],
        "refresh_token":     tokens.get("refresh_token"),
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        "platform_user_id":  channel.get("id"),
        "platform_username": snippet.get("title"),
        "platform_avatar":   snippet.get("thumbnails", {}).get("default", {}).get("url"),
        "extra_config":      {"channel_id": channel.get("id"), "subscriber_count": channel.get("statistics", {}).get("subscriberCount")},
        "shop_domain":       entry["shop_domain"],
    }


async def refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_TOKEN_URL, data={
            "client_id":     settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        })
        resp.raise_for_status()
        tokens = resp.json()
    return {
        "access_token":     tokens["access_token"],
        "token_expires_at": datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600)),
    }


async def post_community(access_token: str, text: str, image_url: str | None = None) -> dict:
    """
    YouTube Community Post.
    Requires channel with 500+ subscribers and YouTube Partner access.
    This uses the undocumented but functional community posts endpoint.
    """
    payload: dict = {
        "snippet": {
            "type":               "textPost",
            "textOriginalContent": text[:5000],
        }
    }
    if image_url:
        payload["snippet"]["type"] = "imagePost"
        payload["snippet"]["imageUrl"] = image_url

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_YT_BASE}/communityPosts",
            json=payload,
            params={"part": "snippet"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
        )
        resp.raise_for_status()
        post = resp.json()

    post_id = post.get("id", "")
    return {
        "platform_post_id":  post_id,
        "platform_post_url": f"https://www.youtube.com/post/{post_id}",
    }
