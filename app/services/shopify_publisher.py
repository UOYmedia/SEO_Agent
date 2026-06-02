"""
Publish AI-generated articles to Shopify via Admin GraphQL API.
REST /articles.json is deprecated in 2025-07+; this uses articleCreate/articleUpdate mutations.
Image is passed as a URL (DALL-E temp URL) — Shopify fetches and hosts it.
"""
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.blog_post import BlogPost, PostStatus


_ARTICLE_FIELDS = """
article {
  id
  handle
  image { url altText }
}
userErrors { field message }
"""

_ARTICLE_CREATE = f"""
mutation ArticleCreate($article: ArticleCreateInput!) {{
  articleCreate(article: $article) {{
    {_ARTICLE_FIELDS}
  }}
}}
"""

_ARTICLE_UPDATE = f"""
mutation ArticleUpdate($id: ID!, $article: ArticleUpdateInput!) {{
  articleUpdate(id: $id, article: $article) {{
    {_ARTICLE_FIELDS}
  }}
}}
"""


class ShopifyPublisher:
    def __init__(
        self,
        shop_domain: Optional[str] = None,
        access_token: Optional[str] = None,
        db=None,
    ):
        self.shop_domain = (shop_domain or settings.SHOPIFY_SHOP_DOMAIN).strip().rstrip("/")
        if access_token:
            self.access_token = access_token
        elif db:
            from app.api.auth_routes import get_store_token
            self.access_token = get_store_token(self.shop_domain, db)
        else:
            self.access_token = settings.SHOPIFY_ACCESS_TOKEN
        self.api_version = settings.SHOPIFY_API_VERSION
        self.endpoint = f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def _headers(self) -> dict:
        return {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _absolute_url(url: str) -> Optional[str]:
        """Turn a relative /static/... path into an absolute URL using APP_URL."""
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        base = (settings.APP_URL or "").strip().rstrip("/")
        if not base:
            return None
        return base + (url if url.startswith("/") else "/" + url)

    async def _gql(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=120.0) as client:
            resp = await client.post(
                self.endpoint,
                json={"query": query, "variables": variables},
            )
            resp.raise_for_status()
            data = resp.json()
            if errors := data.get("errors"):
                raise ValueError(str(errors[0].get("message", errors)))
            return data["data"]

    async def publish_article(
        self,
        post: BlogPost,
        blog_id: int,
        author: str = "SEO Agent",
        published: bool = True,
        image_url: Optional[str] = None,
        image_alt: Optional[str] = None,
    ) -> dict:
        """
        Create or update a Shopify article via GraphQL.
        - post.platform_id exists → articleUpdate (edit in-place on Shopify)
        - no platform_id          → articleCreate (new article)
        Returns the article node dict.
        """
        image_input = None
        if image_url:
            absolute_url = self._absolute_url(image_url)
            if not absolute_url:
                raise ValueError(
                    f"Cannot upload image to Shopify: image_url is relative "
                    f"({image_url}) and APP_URL is not configured. Set APP_URL "
                    f"in settings so Shopify can fetch the image."
                )
            image_input = {"url": absolute_url, "altText": (image_alt or post.title)[:512]}

        # ── UPDATE existing article ──────────────────────────────────────────
        if post.platform_id:
            gid = (
                post.platform_id
                if str(post.platform_id).startswith("gid://")
                else f"gid://shopify/Article/{post.platform_id}"
            )
            update_input: dict = {
                "title":       post.title,
                "body":        post.content_html or "",
                "summary":     post.excerpt_html or "",
                "author":      {"name": author},
                "tags":        post.tags or [],
                "isPublished": published,
            }
            if image_input:
                update_input["image"] = image_input
            data   = await self._gql(_ARTICLE_UPDATE, {"id": gid, "article": update_input})
            result = data["articleUpdate"]
            if result["userErrors"]:
                msgs = "; ".join(f"{e['field']}: {e['message']}" for e in result["userErrors"])
                raise ValueError(f"Shopify userErrors (update): {msgs}")
            return result["article"]

        # ── CREATE new article ───────────────────────────────────────────────
        create_input: dict = {
            "blogId":      f"gid://shopify/Blog/{blog_id}",
            "title":       post.title,
            "body":        post.content_html or "",
            "summary":     post.excerpt_html or "",
            "author":      {"name": author},
            "tags":        post.tags or [],
            "isPublished": published,
        }
        if image_input:
            create_input["image"] = image_input
        data   = await self._gql(_ARTICLE_CREATE, {"article": create_input})
        result = data["articleCreate"]
        if result["userErrors"]:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in result["userErrors"])
            raise ValueError(f"Shopify userErrors (create): {msgs}")
        return result["article"]

    def sync_after_publish(
        self,
        db: Session,
        post: BlogPost,
        shopify_article: dict,
        blog_handle: Optional[str] = None,
    ) -> BlogPost:
        """Update local BlogPost with Shopify IDs, URL and CDN image URL."""
        gid = shopify_article.get("id", "")
        numeric_id = gid.rsplit("/", 1)[-1] if "/" in gid else gid
        handle = shopify_article.get("handle", post.slug or "")
        blog_handle = blog_handle or "news"

        post.platform_id = numeric_id
        post.shop_domain = self.shop_domain
        post.platform_url = f"https://{self.shop_domain}/blogs/{blog_handle}/{handle}"
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime.utcnow()

        img = shopify_article.get("image") or {}
        if img.get("url"):
            post.featured_image_url = img["url"]
            post.featured_image_alt = img.get("altText") or post.title

        db.commit()
        db.refresh(post)
        return post
