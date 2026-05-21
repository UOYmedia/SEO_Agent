"""
Keyword research via Serper.dev (Google Search API).
Fetches SERP results, People Also Ask, and related searches.
"""
from typing import Optional

import httpx

from app.config import settings


class KeywordAnalyzer:
    SERPER_URL = "https://google.serper.dev/search"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.SERPER_API_KEY

    async def _search(self, keyword: str, country: str, language: str, num: int = 10) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                self.SERPER_URL,
                headers={
                    "X-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                json={"q": keyword, "num": num, "gl": country, "hl": language},
            )
            resp.raise_for_status()
            return resp.json()

    def _extract_paa(self, data: dict) -> list[str]:
        return [item["question"] for item in data.get("peopleAlsoAsk", [])]

    def _extract_related(self, data: dict) -> list[str]:
        return [item["query"] for item in data.get("relatedSearches", [])]

    def _extract_top_results(self, data: dict, limit: int = 5) -> list[dict]:
        return [
            {
                "title": r.get("title"),
                "url": r.get("link"),
                "snippet": r.get("snippet"),
                "position": r.get("position"),
            }
            for r in data.get("organic", [])[:limit]
        ]

    async def research(self, keyword: str, country: str = "us", language: str = "en") -> dict:
        """Full keyword research: SERP + PAA + related + top URLs."""
        if not self.api_key:
            # Return mock data when no API key (dev mode)
            return {
                "keyword": keyword,
                "people_also_ask": [
                    f"What is {keyword}?",
                    f"How to use {keyword}?",
                    f"Why is {keyword} important?",
                    f"Best {keyword} tips",
                ],
                "related_searches": [
                    f"{keyword} guide",
                    f"{keyword} tutorial",
                    f"{keyword} examples",
                    f"best {keyword}",
                ],
                "top_results": [],
                "note": "Mock data — set SERPER_API_KEY for real results",
            }

        data = await self._search(keyword, country, language)
        return {
            "keyword": keyword,
            "people_also_ask": self._extract_paa(data),
            "related_searches": self._extract_related(data),
            "top_results": self._extract_top_results(data),
        }
