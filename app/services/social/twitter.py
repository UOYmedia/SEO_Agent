"""Twitter / X publisher — OAuth 2.0 PKCE + tweet creation."""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.social.base import consume_oauth_state, generate_oauth_state, pkce_pair

logger = logging.getLogger(__name__)

_AUTH_URL  = "https://twitter.com/i/oauth2/authorize"
_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
_API_BASE  = "https://api.twitter.com/2"
_SCOPES    = "tweet.read tweet.write users.read offline.access"


def _redirect_uri() -> str:
    return f"{settings.APP_URL}/api/v1/social/oauth/twitter/callback"


def get_auth_url(shop_domain: str) -> str:
    verifier, challenge = pkce_pair()
    state = generate_oauth_state("twitter", shop_domain, code_verifier=verifier)
    params = {
        "response_type": "code",
        "client_id": settings.TWITTER_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "scope": _SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str, state: str) -> dict:
    entry = consume_oauth_state(state)
    if not entry:
        raise ValueError("Invalid or expired OAuth state")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": settings.TWITTER_CLIENT_ID,
                "redirect_uri": _redirect_uri(),
                "code_verifier": entry["code_verifier"],
            },
            auth=(settings.TWITTER_CLIENT_ID, settings.TWITTER_CLIENT_SECRET),
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        profile_resp = await client.get(
            f"{_API_BASE}/users/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            params={"user.fields": "id,name,username,profile_image_url"},
        )
        profile = profile_resp.json().get("data", {}) if profile_resp.is_success else {}

    expires_in = tokens.get("expires_in", 7200)
    return {
        "access_token":      tokens["access_token"],
        "refresh_token":     tokens.get("refresh_token"),
        "token_expires_at":  datetime.utcnow() + timedelta(seconds=expires_in),
        "platform_user_id":  profile.get("id"),
        "platform_username": profile.get("username") or profile.get("name"),
        "platform_avatar":   profile.get("profile_image_url"),
        "shop_domain":       entry["shop_domain"],
    }


async def refresh_tokens(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.TWITTER_CLIENT_ID,
            },
            auth=(settings.TWITTER_CLIENT_ID, settings.TWITTER_CLIENT_SECRET),
        )
        resp.raise_for_status()
        tokens = resp.json()
    expires_in = tokens.get("expires_in", 7200)
    return {
        "access_token":     tokens["access_token"],
        "refresh_token":    tokens.get("refresh_token", refresh_token),
        "token_expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
    }


async def post_tweet(access_token: str, text: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{_API_BASE}/tweets",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"text": text[:280]},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

    tweet_id = data.get("id", "")
    return {
        "platform_post_id":  tweet_id,
        "platform_post_url": f"https://x.com/i/web/status/{tweet_id}",
    }
