"""
Keyword search volume via DataForSEO.

Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in env to enable.
"""
import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"


def is_configured() -> bool:
    return bool(settings.DATAFORSEO_LOGIN and settings.DATAFORSEO_PASSWORD)


async def get_search_volumes(
    keywords: list[str],
    language_code: str = "en",
    location_code: int = 2840,  # 2840 = United States
) -> dict[str, dict]:
    """
    Fetch monthly search volume for a list of keywords.
    Returns {keyword: {volume, competition_index, cpc}} or raises on error.
    Returns {} if DataForSEO not configured.
    """
    if not is_configured():
        return {}
    if not keywords:
        return {}

    results: dict[str, dict] = {}
    for i in range(0, len(keywords), 100):
        chunk = keywords[i : i + 100]
        payload = [{
            "location_code": location_code,
            "language_code": language_code,
            "keywords": chunk,
        }]
        async with httpx.AsyncClient(
            auth=(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD),
            timeout=30.0,
        ) as client:
            resp = await client.post(_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()

        # DataForSEO top-level status
        top_code = data.get("status_code")
        if top_code and top_code != 20000:
            raise ValueError(f"DataForSEO error {top_code}: {data.get('status_message')}")

        for task in data.get("tasks", []):
            task_code = task.get("status_code")
            if task_code and task_code != 20000:
                logger.warning("DataForSEO task error %s: %s", task_code, task.get("status_message"))
                continue
            for item in task.get("result") or []:
                kw = item.get("keyword", "")
                if kw:
                    results[kw] = {
                        "volume": item.get("search_volume"),
                        # competition is a string HIGH/MEDIUM/LOW; competition_index is 0-100
                        "competition": item.get("competition_index"),
                        "cpc": item.get("cpc"),
                    }

    return results


async def test_connection(keyword: str = "seo") -> dict:
    """Test DataForSEO credentials and return raw task info for debugging."""
    if not is_configured():
        return {"ok": False, "error": "DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD not set in environment"}

    payload = [{"location_code": 2840, "language_code": "en", "keywords": [keyword]}]
    try:
        async with httpx.AsyncClient(
            auth=(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD),
            timeout=30.0,
        ) as client:
            resp = await client.post(_ENDPOINT, json=payload)
        raw = resp.json()
        task = (raw.get("tasks") or [{}])[0]
        result_item = ((task.get("result") or [{}])[0])
        return {
            "ok": resp.status_code == 200 and raw.get("status_code") == 20000,
            "http_status": resp.status_code,
            "api_status_code": raw.get("status_code"),
            "api_status_message": raw.get("status_message"),
            "task_status_code": task.get("status_code"),
            "task_status_message": task.get("status_message"),
            "sample_result": result_item if result_item else None,
            "cost": raw.get("cost"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
