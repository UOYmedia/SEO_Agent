"""Threads publisher — Meta Threads API (separate from Facebook Graph)."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state

logger = logging.getLogger(__name__)

_AUTH_URL       = "https://threads.net/oauth/authorize"
_TOKEN_URL      = "https://graph.threads.net/oauth/access_token"
_LONG_TOKEN_URL = "https://graph.threads.net/access_token"
_API_BASE       = "https://graph.threads.net/v1.0"
# Threads uses the same Meta app credentials as Facebook


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/threads/callback"


def get_auth_url(shop_domain: str) -> str:
    state = generate_oauth_state("threads", shop_domain)
    return f"{_AUTH_URL}?" + urlencode({
        "client_id":     settings.FACEBOOK_APP_ID,
        "redirect_uri":  _redirect_uri(),
        "scope":         "threads_basic,threads_content_publish",
        "response_type": "code",
        "state":         state,
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        # Short-lived token
        short_resp = await client.post(_TOKEN_URL, data={
            "client_id":     settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "grant_type":    "authorization_code",
            "redirect_uri":  _redirect_uri(),
            "code":          code,
        })
        short_resp.raise_for_status()
        short = short_resp.json()
        user_id = str(short["user_id"])

        # Long-lived token (60-day)
        ll_resp = await client.get(_LONG_TOKEN_URL, params={
            "grant_type":    "th_exchange_token",
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "access_token":  short["access_token"],
        })
        ll_resp.raise_for_status()
        long = ll_resp.json()

        me_resp = await client.get(f"{_API_BASE}/{user_id}", params={
            "fields":       "id,username,name,threads_profile_picture_url",
            "access_token": long["access_token"],
        })
        me = me_resp.json() if me_resp.is_success else {}

    expires_in = long.get("expires_in", 5184000)  # ~60 days default
    return {
        "access_token":      long["access_token"],
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        "platform_user_id":  user_id,
        "platform_username": me.get("username") or me.get("name"),
        "platform_avatar":   me.get("threads_profile_picture_url"),
        "shop_domain":       entry["shop_domain"],
    }


async def refresh_tokens(access_token: str) -> dict:
    """Threads uses token refresh via th_refresh_token grant."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_LONG_TOKEN_URL, params={
            "grant_type":   "th_refresh_token",
            "access_token": access_token,
        })
        resp.raise_for_status()
        tokens = resp.json()
    expires_in = tokens.get("expires_in", 5184000)
    return {
        "access_token":     tokens["access_token"],
        "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
    }


async def create_post(
    access_token: str,
    user_id: str,
    text: str,
    image_url: str | None = None,
) -> dict:
    """Two-step: create container → publish."""
    params = {"access_token": access_token}

    if image_url:
        container_payload = {
            "media_type": "IMAGE",
            "image_url":  image_url,
            "text":       text[:500],
        }
    else:
        container_payload = {
            "media_type": "TEXT",
            "text":       text[:500],
        }

    async with httpx.AsyncClient(timeout=30) as client:
        c_resp = await client.post(
            f"{_API_BASE}/{user_id}/threads",
            params=params,
            json=container_payload,
        )
        c_resp.raise_for_status()
        creation_id = c_resp.json()["id"]

        p_resp = await client.post(
            f"{_API_BASE}/{user_id}/threads_publish",
            params={**params, "creation_id": creation_id},
        )
        p_resp.raise_for_status()
        post_id = p_resp.json()["id"]

    username = ""  # caller can provide if needed
    return {
        "platform_post_id":  post_id,
        "platform_post_url": f"https://www.threads.net/post/{post_id}",
    }
