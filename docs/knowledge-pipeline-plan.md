# Knowledge Pipeline — PR B & PR C Plan

Roadmap để tiếp tục phần "AI tự đúc rút insight từ feedback/history → đề xuất Knowledge Base".

PR A (foundation) đã merge: `ArticleEditHistory` + timeline UI trong blog view + feedback có `user_id`.

---

## PR B — Daily insight aggregator (feedback → KB suggestions)

### Mục tiêu
Hằng ngày tổng hợp **feedback của user trên blog posts** trong 24h gần nhất → Claude phân tích → đẻ ra các `KnowledgeSuggestion` ngắn → admin review trong queue → approve thì convert thành `KnowledgeItem` (đi vào KB hiện hành).

User đã chốt: **input chỉ là feedback** (không dùng rewrite history, không dùng GSC ở PR này).

### Data model

File mới: `app/models/knowledge_suggestion.py`

```python
class SuggestionStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class SuggestionKind:
    BRAND = "brand"      # đặc thù cho 1 shop_domain
    GENERAL = "general"  # áp dụng được cho mọi store

class SuggestionSource:
    FEEDBACK = "feedback"        # PR B chỉ có source này
    REWRITE = "rewrite_pattern"  # để dành cho PR C
    TREND = "trend"              # GSC, để dành
    ANALYSIS = "analysis"

class KnowledgeSuggestion(Base):
    __tablename__ = "knowledge_suggestions"
    id              = Column(Integer, primary_key=True)
    shop_domain     = Column(String(255), nullable=True, index=True)  # null = general
    kind            = Column(String(20), default=SuggestionKind.BRAND, index=True)
    source          = Column(String(30), default=SuggestionSource.FEEDBACK, index=True)
    title           = Column(String(500))
    content_md      = Column(Text)               # 1–3 câu insight
    rationale       = Column(Text)               # vì sao đề xuất (trích feedback cụ thể)
    evidence_refs   = Column(JSON)               # [{type:"feedback", id:42, post_id:7, rating:2}]
    status          = Column(String(20), default=SuggestionStatus.PENDING, index=True)
    reviewed_by     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at     = Column(DateTime, nullable=True)
    rejection_note  = Column(Text, nullable=True)
    promoted_item_id= Column(Integer, ForeignKey("knowledge_items.id", ondelete="SET NULL"), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)
    run_id          = Column(String(64), index=True)  # group theo lần chạy (uuid)
```

Đăng ký trong `app/main.py` (`from app.models import knowledge_suggestion as _ks`).

Migration ALTER không cần — `create_all` tạo bảng mới. Không cần backfill.

### Service: `app/services/insights_aggregator.py`

```python
class InsightsAggregator:
    def __init__(self, claude_client): ...

    def collect_feedback(self, db, since: datetime, shop_domain: Optional[str]) -> list[dict]:
        """Pull ArticleFeedback rows joined with BlogPost title/focus_keyword
        for the last 24h (or `since`). Group per shop if shop_domain is None."""

    def build_prompt(self, feedbacks_by_shop: dict) -> str:
        """Cho Claude. Yêu cầu trả về JSON array:
        [{title, content_md (1-3 câu actionable), rationale, kind: brand|general,
          shop_domain (nếu brand), evidence_ids: [feedback_id,...]}]
        Hướng dẫn:
        - Gộp các feedback có pattern lặp lại
        - Phân loại brand vs general:
          * brand nếu insight chỉ áp dụng cho 1 shop (tone, product names,…)
          * general nếu áp dụng được liên store (vd: "always include FAQ section")
        - Bỏ qua noise (rating 5 không có notes)
        """

    async def run(self, db, since=None, shop_domain=None) -> list[KnowledgeSuggestion]:
        """Main entry. Returns created suggestions; commits in batch."""
```

Dùng `anthropic` (đã có model `claude-opus-4-7` trong config). Nếu chưa có anthropic SDK trong `requirements.txt`, thêm `anthropic==0.x`.

### Scheduler

Có sẵn `apscheduler==3.10.4` trong requirements nhưng chưa khởi tạo. Plan:

1. Tạo `app/services/scheduler.py`:
   ```python
   from apscheduler.schedulers.asyncio import AsyncIOScheduler
   scheduler = AsyncIOScheduler()

   def register_jobs():
       scheduler.add_job(run_daily_insights, "cron", hour=2, minute=0, id="daily_insights")
   ```
2. Khởi động trong `app/main.py` `lifespan`:
   ```python
   from app.services.scheduler import scheduler, register_jobs
   register_jobs()
   scheduler.start()
   yield
   scheduler.shutdown()
   ```
3. `run_daily_insights()` mở session, gọi `InsightsAggregator.run()` cho từng shop.

### Endpoints (admin only)

File mới hoặc thêm vào `app/api/knowledge_routes.py`:

```
GET  /api/v1/knowledge/suggestions?status=pending&shop_domain=&kind=
        → list paginated
POST /api/v1/knowledge/suggestions/run
        body: {shop_domain?: string, since_hours?: 24}
        → trigger manual aggregation; require_admin
PUT  /api/v1/knowledge/suggestions/{id}/approve
        body: {kind_override?, shop_domain_override?, content_md_override?}
        → tạo KnowledgeItem (status=APPROVED), set promoted_item_id, status=APPROVED
PUT  /api/v1/knowledge/suggestions/{id}/reject
        body: {note}
        → status=REJECTED, save note
```

Tất cả `require_admin`. Approve flow tái sử dụng `KnowledgeItem` đã có (set `source_type="feedback_insight"` hoặc tương tự).

### UI

Thêm tab "Suggestions" trong `renderKnowledge()` (hoặc trang riêng):

```
┌─ [Pending: 12] [Approved] [Rejected] ─── [▶ Run now] ─┐
│ Each card:                                              │
│  🏷 brand (flagwix.myshopify.com)   2 hours ago         │
│  Title: "Always end FAQ with CTA to product page"       │
│  Insight: "Users rated low when FAQ doesn't link back…" │
│  Rationale: 3 feedback (avg 2.3/5) cite missing CTAs    │
│  Evidence: [post #42] [post #57] [post #61]             │
│  [✓ Approve as brand] [✓ Approve as general] [✗ Reject] │
└─────────────────────────────────────────────────────────┘
```

Filter theo `shop_domain` (admin có store selector). Mỗi card expand được để xem feedback gốc.

### Test plan (PR B)
- Unit: `collect_feedback` lọc đúng window 24h + shop filter
- Mock Claude: trả JSON cố định, verify suggestions được tạo đúng kind
- Integration: chạy `run()` với DB seed (10 feedbacks lẫn shop) → assert số suggestions + status pending
- Manual: chạy `POST /suggestions/run`, approve 1, verify KnowledgeItem mới tạo có `status=approved`

---

## PR C — KB brand/general + weekly classifier review

### Mục tiêu
Knowledge Base hiện đang dùng `shop_domain IS NULL` để ngầm phân loại general. PR C **explicit hóa** brand vs general + weekly job dùng AI để:
- Đề xuất reclassify items đặt sai category
- Gợi ý merge/dedupe items trùng lặp
- Admin review queue trước khi commit

### Data model

#### 1. Thêm `category` vào `KnowledgeItem`

```python
class KnowledgeCategory:
    BRAND = "brand"
    GENERAL = "general"

# trong KnowledgeItem
category = Column(String(20), default=KnowledgeCategory.BRAND, index=True)
```

Migration trong `app/database.py::_migrate_columns`:
```python
add_cols("knowledge_items", [("category", "VARCHAR(20)")])
# Backfill: shop_domain IS NULL → general, ngược lại → brand
conn.execute(text("""
    UPDATE knowledge_items
    SET category = CASE WHEN shop_domain IS NULL THEN 'general' ELSE 'brand' END
    WHERE category IS NULL
"""))
```

#### 2. New model `KnowledgeReviewAction`

```python
class ReviewActionKind:
    RECLASSIFY = "reclassify"   # đổi brand ↔ general
    MERGE      = "merge"        # gộp N items → 1
    DUPLICATE  = "duplicate"    # đánh dấu để xóa

class ReviewActionStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class KnowledgeReviewAction(Base):
    __tablename__ = "knowledge_review_actions"
    id              = Column(Integer, primary_key=True)
    kind            = Column(String(20), index=True)
    target_item_id  = Column(Integer, ForeignKey("knowledge_items.id"))
    payload         = Column(JSON)
        # reclassify: {from: brand, to: general}
        # merge:      {merge_ids: [12, 34], primary_id: 12, merged_content_md: "..."}
        # duplicate:  {duplicate_of: 12}
    rationale       = Column(Text)              # AI giải thích
    confidence      = Column(Float)             # 0–1
    status          = Column(String(20), default="pending", index=True)
    reviewed_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at     = Column(DateTime, nullable=True)
    run_id          = Column(String(64), index=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
```

### Service: `app/services/knowledge_classifier.py`

```python
class KnowledgeClassifier:
    async def run_weekly_review(self, db) -> list[KnowledgeReviewAction]:
        """
        1. Load all approved KnowledgeItem (limit ~500 per shop)
        2. For each, ask Claude:
           - is category correct? (brand vs general)
           - is this a near-duplicate of another item? (use embedding cosine)
        3. Generate ReviewAction rows for items needing change
        4. Cluster near-dupes via embedding similarity (> 0.92) → merge action
        """

    def find_duplicates(self, items, threshold=0.92) -> list[list[int]]:
        """Greedy clustering on embeddings. Returns groups of similar IDs."""
```

Tận dụng `embedding` đã có sẵn trên `KnowledgeItem`.

### Scheduler

Cron weekly: `scheduler.add_job(run_weekly_review, "cron", day_of_week="mon", hour=3, id="weekly_kb_review")`

### Endpoints (admin only)

```
GET  /api/v1/knowledge/review/actions?status=pending&kind=
POST /api/v1/knowledge/review/run        → trigger manual
PUT  /api/v1/knowledge/review/{id}/approve
        # reclassify: cập nhật item.category
        # merge: tạo 1 item primary (cộng dồn content_md), xóa các item phụ
        # duplicate: xóa item duplicate
PUT  /api/v1/knowledge/review/{id}/reject
```

### UI

Tab "KB Review" trong `renderKnowledge()`:

```
[ Reclassify (5) ] [ Merge (3) ] [ Duplicates (2) ]   [▶ Run now]

Reclassify card:
  Item #42 "Always link to product pages from FAQ"
  Currently: brand (flagwix)
  Suggested: general (confidence 0.87)
  Why: "Pattern applies to all e-commerce blogs, not Flagwix-specific"
  [✓ Apply] [✗ Keep brand]

Merge card:
  Suggested merge of 3 items → #42 (primary):
    #42 "FAQ must link to product"
    #56 "Always add product links in FAQ section"
    #78 "FAQs should have CTAs to products"
  AI-merged content preview: [...]
  [✓ Apply merge] [✗ Keep separate]
```

### Test plan (PR C)
- Migration: backfill chạy → existing items có category đúng (`shop_domain` rỗng → general, có → brand)
- `find_duplicates` với embeddings giả → trả đúng cluster
- Mock Claude: reclassify decision → action được tạo
- Approve merge → items phụ bị xóa, primary giữ content gộp
- Approve reclassify → `KnowledgeItem.category` cập nhật

---

## Phụ thuộc giữa các PR

- **PR B độc lập với PR C** — có thể làm song song
- Cả hai đều cần **APScheduler bootstrap** trong `lifespan`. PR nào làm trước thì add chung helper `app/services/scheduler.py`; PR sau chỉ thêm job.
- Cả hai đều cần **Anthropic SDK** trong `requirements.txt`. Hiện chỉ có `openai`. Thêm `anthropic==0.x.x` ở PR đầu tiên cần dùng.
- **Frontend**: cả hai dùng chung pattern "review queue card với approve/reject". Có thể tách thành helper `renderReviewCard()` ở PR B rồi tái dùng ở PR C.

## Bảo mật / quyền

- Tất cả endpoint review/approve **chỉ admin**
- Suggestion với `kind=brand` + `shop_domain=X` → KB item phải `shop_domain=X` (auto-scope theo logic hiện có ở PR cũ)
- Trigger thủ công `/run` cho cả 2 đều `require_admin`

## File mới cần tạo (checklist)

PR B:
- [ ] `app/models/knowledge_suggestion.py`
- [ ] `app/services/insights_aggregator.py`
- [ ] `app/services/scheduler.py` (nếu chưa có)
- [ ] `app/api/knowledge_routes.py` (thêm endpoints `/suggestions/*`)
- [ ] `app/static/index.html` (tab Suggestions UI)
- [ ] `requirements.txt` (thêm `anthropic`)
- [ ] `app/main.py` (register model + start scheduler)

PR C:
- [ ] `app/models/knowledge_item.py` (thêm `category`)
- [ ] `app/models/knowledge_review.py`
- [ ] `app/services/knowledge_classifier.py`
- [ ] `app/database.py` (`_migrate_columns` thêm cột + backfill)
- [ ] `app/services/scheduler.py` (thêm weekly job)
- [ ] `app/api/knowledge_routes.py` (thêm `/review/*`)
- [ ] `app/static/index.html` (tab KB Review UI)
- [ ] `app/main.py` (register model mới)
