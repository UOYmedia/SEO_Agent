"""Pinterest publisher — API v5 OAuth 2.0."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state

logger = logging.getLogger(__name__)

_AUTH_URL  = "https://www.pinterest.com/oauth/"
_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
_API_BASE  = "https://api.pinterest.com/v5"
_SCOPES    = "boards:read,pins:read,pins:write,user_accounts:read"


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/pinterest/callback"


def get_auth_url(shop_domain: str) -> str:
    state = generate_oauth_state("pinterest", shop_domain)
    return f"{_AUTH_URL}?" + urlencode({
        "client_id":     settings.PINTEREST_APP_ID,
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         _SCOPES,
        "state":         state,
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": _redirect_uri(),
            },
            auth=(settings.PINTEREST_APP_ID, settings.PINTEREST_APP_SECRET),
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}
        me = (await client.get(f"{_API_BASE}/user_account", headers=hdrs)).json()
        boards = (await client.get(f"{_API_BASE}/boards", headers=hdrs, params={"page_size": 50})).json().get("items", [])

    expires_in = tokens.get("expires_in", 2592000)  # Pinterest default 30 days
    return {
        "access_token":      tokens["access_token"],
        "refresh_token":     tokens.get("refresh_token"),
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        "platform_user_id":  me.get("id") or me.get("username"),
        "platform_username": me.get("username"),
        "platform_avatar":   me.get("profile_image"),
        "boards":            boards,
        "shop_domain":       entry["shop_domain"],
    }


async def refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(settings.PINTEREST_APP_ID, settings.PINTEREST_APP_SECRET),
        )
        resp.raise_for_status()
        tokens = resp.json()
    expires_in = tokens.get("expires_in", 2592000)
    return {
        "access_token":     tokens["access_token"],
        "refresh_token":    tokens.get("refresh_token", refresh_token),
        "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
    }


async def create_pin(
    access_token: str,
    board_id: str,
    title: str,
    description: str,
    link: str,
    image_url: str | None = None,
) -> dict:
    payload: dict = {
        "board_id":    board_id,
        "title":       title[:100],
        "description": description[:500],
        "link":        link,
    }
    if image_url:
        payload["media_source"] = {"source_type": "image_url", "url": image_url}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_API_BASE}/pins",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
        )
        resp.raise_for_status()
        pin = resp.json()

    pin_id = pin.get("id", "")
    return {
        "platform_post_id":  pin_id,
        "platform_post_url": f"https://www.pinterest.com/pin/{pin_id}/",
    }
