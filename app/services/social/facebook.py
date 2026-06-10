"""Facebook Fanpage publisher — Meta Graph API OAuth 2.0."""
import logging
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state

logger = logging.getLogger(__name__)

_GRAPH    = "https://graph.facebook.com/v21.0"
_AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
_SCOPES   = "pages_manage_posts,pages_read_engagement,pages_show_list,business_management"


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/facebook/callback"


def get_auth_url(shop_domain: str) -> str:
    state = generate_oauth_state("facebook", shop_domain)
    return f"{_AUTH_URL}?" + urlencode({
        "client_id":    settings.FACEBOOK_APP_ID,
        "redirect_uri": _redirect_uri(),
        "scope":        _SCOPES,
        "state":        state,
    })


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=20) as client:
        # Short-lived → long-lived user token
        short = await client.get(f"{_GRAPH}/oauth/access_token", params={
            "client_id":     settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "redirect_uri":  _redirect_uri(),
            "code":          code,
        })
        short.raise_for_status()
        short_token = short.json()["access_token"]

        ll = await client.get(f"{_GRAPH}/oauth/access_token", params={
            "grant_type":       "fb_exchange_token",
            "client_id":        settings.FACEBOOK_APP_ID,
            "client_secret":    settings.FACEBOOK_APP_SECRET,
            "fb_exchange_token": short_token,
        })
        ll.raise_for_status()
        long_token = ll.json()["access_token"]

        # User profile
        me = (await client.get(f"{_GRAPH}/me", params={
            "access_token": long_token,
            "fields": "id,name,picture",
        })).json()

        # Managed pages (each has its own permanent page-access-token)
        pages_resp = await client.get(f"{_GRAPH}/me/accounts", params={
            "access_token": long_token,
            "fields": "id,name,access_token,picture",
        })
        pages = pages_resp.json().get("data", []) if pages_resp.is_success else []

    return {
        "access_token":      long_token,
        "platform_user_id":  me.get("id"),
        "platform_username": me.get("name"),
        "platform_avatar":   me.get("picture", {}).get("data", {}).get("url"),
        "pages":             pages,
        "shop_domain":       entry["shop_domain"],
    }


async def post_to_page(
    page_token: str,
    page_id: str,
    text: str,
    link: str | None = None,
    image_url: str | None = None,
) -> dict:
    payload: dict = {"access_token": page_token}

    async with httpx.AsyncClient(timeout=30) as client:
        if image_url:
            # Photo post with caption
            resp = await client.post(f"{_GRAPH}/{page_id}/photos", data={
                **payload,
                "url":     image_url,
                "caption": text,
            })
        else:
            # Link / text post
            if link:
                payload["link"] = link
            payload["message"] = text
            resp = await client.post(f"{_GRAPH}/{page_id}/feed", data=payload)

        resp.raise_for_status()
        post_id = resp.json().get("id", "")

    # post_id is "{page_id}_{post_id}" — build URL
    parts = post_id.split("_", 1)
    fb_url = f"https://www.facebook.com/permalink.php?story_fbid={parts[-1]}&id={parts[0]}" if len(parts) == 2 else f"https://www.facebook.com/{post_id}"
    return {
        "platform_post_id":  post_id,
        "platform_post_url": fb_url,
    }
