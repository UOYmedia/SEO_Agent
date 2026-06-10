"""AI-powered content formatter: adapts a blog article for each social platform."""
import logging
import re
from typing import Optional

from app.agents.base import get_client, get_model

logger = logging.getLogger(__name__)

# ── Per-platform copy instructions ───────────────────────────────────────────

_INSTRUCTIONS: dict[str, str] = {
    "twitter": (
        "Write a Twitter/X post. Hard limit: 240 characters (the URL takes 23 chars). "
        "Format: punchy hook → 1 key insight → 2-3 relevant hashtags → URL at the very end. "
        "Be concise, provocative, or surprising."
    ),
    "facebook": (
        "Write a Facebook page post (100-300 words). "
        "Open with a question or bold statement to stop the scroll. "
        "Give 3-4 insight sentences. End with a CTA + URL. "
        "Add 3-5 hashtags on a new line. Aim for comments by closing with a question."
    ),
    "pinterest": (
        "Write a Pinterest pin description (150-300 characters). "
        "Pinterest is a search engine — embed primary keywords naturally. "
        "Describe what the reader will learn, who it's for. "
        "End with the URL. No hashtags — keywords only."
    ),
    "threads": (
        "Write a Threads post (max 500 characters). "
        "Conversational and authentic, like a mini tweet-thread opener. "
        "One key insight + URL + 1-2 hashtags. Short paragraphs."
    ),
    "linkedin": (
        "Write a LinkedIn post (150-350 words). "
        "Open with a bold professional insight. "
        "Give 3 numbered key takeaways. "
        "Close with a thoughtful question inviting professional discussion + URL. "
        "Add 3-5 professional hashtags on the last line."
    ),
    "tiktok": (
        "Write a TikTok video caption (first 100 chars visible before 'More'). "
        "Start with a curiosity-gap hook or bold claim. "
        "List 2-3 quick value points. "
        "End with 'Link in bio 🔗' + 5-8 trending hashtags including #fyp. "
        "High energy, emoji-rich."
    ),
    "youtube": (
        "Write a YouTube Community post (200-400 words). "
        "Greet subscribers, share 3 key insights from the article as a mini-read, "
        "ask an engaging question for comments, then link to the full article. "
        "Warm and conversational, like a creator talking to their audience."
    ),
}

_CHAR_LIMITS = {
    "twitter": 280, "facebook": 5000, "pinterest": 500,
    "threads": 500, "linkedin": 3000, "tiktok": 2200, "youtube": 5000,
}


def _strip_html(html: str, max_chars: int = 3000) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


async def format_for_platform(
    platform: str,
    title: str,
    content_html: str,
    article_url: str,
    keywords: list[str],
    image_url: Optional[str] = None,
) -> dict:
    """
    Returns dict with keys:
      text        — ready-to-publish copy
      image_url   — pass-through of input image_url
      hashtags    — list[str] extracted from text
      char_count  — len(text)
    """
    instructions = _INSTRUCTIONS.get(platform, _INSTRUCTIONS["facebook"])
    plain = _strip_html(content_html)
    kw_str = ", ".join(keywords[:12]) if keywords else ""

    system = (
        f"You are a social media content strategist who crafts platform-optimised posts "
        f"from blog articles to drive traffic and build external SEO signals.\n\n"
        f"Platform: {platform.upper()}\n"
        f"Task: {instructions}\n\n"
        f"Always include this exact URL in the post: {article_url}\n"
        f"Primary SEO keywords to use as hashtags/copy: {kw_str}\n\n"
        f"Output ONLY the final post text — no preamble, no meta-commentary."
    )
    user = f"Article title: {title}\n\nContent:\n{plain}"

    text = ""
    try:
        client = get_client()
        model = get_model("planning")
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.75,
            max_tokens=800,
        )
        text = resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("Social formatter error (%s): %s", platform, exc)

    if not text:
        kw_tags = " ".join(f"#{k.replace(' ', '')}" for k in keywords[:3])
        text = f"📖 {title}\n\n{article_url}\n\n{kw_tags}"

    # Enforce hard char limit
    limit = _CHAR_LIMITS.get(platform, 5000)
    if len(text) > limit:
        text = text[: limit - 4] + " ..."

    hashtags = re.findall(r"#(\w+)", text)
    return {
        "text": text,
        "image_url": image_url,
        "hashtags": hashtags,
        "char_count": len(text),
    }
