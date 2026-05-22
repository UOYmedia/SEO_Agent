from typing import Optional
from pydantic import BaseModel, Field


class PublishToShopifyRequest(BaseModel):
    blog_id: int = Field(..., description="Shopify blog ID (channel platform_id)")
    shop_domain: Optional[str] = Field(None, description="Target store domain")
    author: str = "SEO Agent"
    published: bool = True
