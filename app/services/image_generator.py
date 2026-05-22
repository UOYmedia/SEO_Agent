"""
DALL-E 3 image generator for SEO blog banners.
Supports b64_json (for Shopify upload) and url (for preview).
"""
from openai import OpenAI

from app.config import settings


class ImageGenerator:
    # gpt-image-1 valid sizes
    VALID_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}

    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def generate(
        self,
        prompt: str,
        size: str = "1536x1024",
        response_format: str = "url",  # kept for call-site compat, ignored
    ) -> dict:
        """
        Generate a banner with gpt-image-1.
        Returns {"url": str, "revised_prompt": str}.
        """
        if size not in self.VALID_SIZES:
            size = "1536x1024"

        response = self.client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            quality="medium",
            n=1,
        )
        item = response.data[0]
        return {
            "url": getattr(item, "url", None),
            "revised_prompt": getattr(item, "revised_prompt", prompt),
        }
