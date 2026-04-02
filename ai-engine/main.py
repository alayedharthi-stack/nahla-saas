"""
Nahla AI Engine  –  port 8002
Receives a tenant + customer message and returns a context-aware reply.

The engine works in three stages:
  1. Load tenant context  – products, active coupons, store policy from the DB.
  2. Build a prompt       – combines context with the customer message.
  3. Generate a reply     – calls the configured LLM provider (OpenAI-compatible).
     Falls back to a rule-based responder when no API key is set (dev mode).
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Make the shared database layer importable regardless of working directory
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Coupon, Integration, KnowledgePolicy, Product, Tenant, TenantSettings
from database.session import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-engine")

# ── LLM Configuration ────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

app = FastAPI(
    title="Nahla AI Engine",
    description="Context-aware AI response generation for WhatsApp commerce.",
    version="1.0.0",
)


# ── Request / Response Schemas ────────────────────────────────────────────────

class AIRequest(BaseModel):
    tenant: str          # display_phone_number or tenant identifier from WhatsApp metadata
    phone: str           # customer phone number
    message: str         # incoming customer message text
    tenant_id: Optional[int] = None   # numeric DB tenant id if already resolved


class AIResponse(BaseModel):
    response: str
    tenant: str
    model: str


# ── Tenant Context Loader ────────────────────────────────────────────────────

def _load_tenant_context(tenant_identifier: str, tenant_id: Optional[int]) -> Dict[str, Any]:
    """
    Load store context from the database to ground the AI response.
    Returns a dict with: store_name, products, coupons, policy, branding.
    """
    db = SessionLocal()
    try:
        tenant: Optional[Tenant] = None

        if tenant_id:
            tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()

        # Fallback: resolve tenant by registered WhatsApp number.
        # This is a lookup (not a data query), so no tenant_id scoping is needed —
        # the result itself determines which tenant owns this number.
        # Guard: only resolve to active tenants.
        if not tenant:
            from database.models import WhatsAppNumber
            wa = db.query(WhatsAppNumber).filter(
                WhatsAppNumber.number.contains(tenant_identifier)
            ).first()
            if wa:
                tenant = db.query(Tenant).filter(
                    Tenant.id == wa.tenant_id,
                    Tenant.is_active == True,
                ).first()

        if not tenant:
            return {"store_name": "our store", "products": [], "coupons": [], "policy": {}, "branding": {}}

        # Products (limit to 30 most relevant for prompt size)
        products: List[Product] = (
            db.query(Product)
            .filter(Product.tenant_id == tenant.id)
            .limit(30)
            .all()
        )
        product_lines = [
            f"- {p.title} | SAR {p.price} | SKU: {p.sku or 'N/A'}"
            for p in products
        ]

        # Active coupons
        coupons: List[Coupon] = (
            db.query(Coupon)
            .filter(Coupon.tenant_id == tenant.id)
            .limit(10)
            .all()
        )
        coupon_lines = [
            f"- {c.code}: {c.discount_type} {c.discount_value}"
            for c in coupons
        ]

        # Knowledge policy (AI permissions)
        policy_row: Optional[KnowledgePolicy] = (
            db.query(KnowledgePolicy)
            .filter(KnowledgePolicy.tenant_id == tenant.id)
            .first()
        )
        policy = {}
        if policy_row:
            policy = {
                "allowed_categories": policy_row.allowed_categories or [],
                "blocked_categories": policy_row.blocked_categories or [],
                "escalation_rules":   policy_row.escalation_rules   or {},
            }

        # Branding / settings
        settings: Optional[TenantSettings] = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant.id)
            .first()
        )
        branding = {}
        if settings:
            branding = {
                "show_nahla_branding": settings.show_nahla_branding,
                "branding_text":       settings.branding_text,
            }

        return {
            "store_name":    tenant.name,
            "store_address": tenant.store_address or "",
            "products":      product_lines,
            "coupons":       coupon_lines,
            "policy":        policy,
            "branding":      branding,
        }

    finally:
        db.close()


# ── Prompt Builder ───────────────────────────────────────────────────────────

def _build_system_prompt(context: Dict[str, Any]) -> str:
    blocked = context["policy"].get("blocked_categories", [])
    blocked_note = (
        f"\nDo NOT discuss: {', '.join(blocked)}." if blocked else ""
    )

    products_section = (
        "\n".join(context["products"]) if context["products"]
        else "No products loaded yet."
    )
    coupons_section = (
        "\n".join(context["coupons"]) if context["coupons"]
        else "No active coupons."
    )

    return f"""You are a smart, friendly WhatsApp sales assistant for "{context['store_name']}".
Your job is to help customers discover products, answer questions, and guide them to purchase.

Store location: {context['store_address'] or 'Available on request'}

AVAILABLE PRODUCTS:
{products_section}

ACTIVE COUPONS:
{coupons_section}

RULES:
- Reply in the same language the customer uses (Arabic or English).
- Be concise — WhatsApp messages should be short and clear.
- If a customer wants to order, ask for their name, address, and preferred payment method.
- Never fabricate products, prices, or policies.
- If you cannot help, politely offer to connect them with a human agent.{blocked_note}
- Do not include any branding footer — that is added separately."""


# ── LLM Call ─────────────────────────────────────────────────────────────────

async def _call_llm(system_prompt: str, user_message: str) -> str:
    """Call an OpenAI-compatible chat completions endpoint."""
    if not OPENAI_API_KEY:
        # Dev/fallback mode — rule-based responses
        return _rule_based_response(user_message)

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_message},
        ],
        "max_tokens": 400,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{OPENAI_API_BASE}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def _rule_based_response(message: str) -> str:
    """
    Minimal rule-based fallback used when no LLM API key is configured.
    Covers the most common WhatsApp commerce intents.
    """
    text = message.strip().lower()

    greetings = {"hi", "hello", "hey", "مرحبا", "أهلا", "اهلا", "سلام", "هلا"}
    if any(g in text for g in greetings):
        return "مرحباً! 👋 كيف أقدر أساعدك اليوم؟\n\nHello! 👋 How can I help you today?"

    if any(w in text for w in ("منتج", "product", "price", "سعر", "catalog", "كتالوج")):
        return (
            "يسعدني مساعدتك! 🛍️ يمكنك تصفح منتجاتنا أو أخبرني بما تبحث عنه.\n\n"
            "Happy to help! 🛍️ Browse our catalog or tell me what you're looking for."
        )

    if any(w in text for w in ("order", "طلب", "اطلب", "اشتري", "buy", "purchase")):
        return (
            "ممتاز! 🎉 لإتمام الطلب، أحتاج منك:\n"
            "1. اسمك الكريم\n2. عنوان التوصيل\n3. طريقة الدفع المفضلة\n\n"
            "Great! 🎉 To complete your order I need:\n"
            "1. Your name\n2. Delivery address\n3. Preferred payment method"
        )

    if any(w in text for w in ("coupon", "discount", "كوبون", "خصم", "offer", "عرض")):
        return (
            "لدينا عروض وخصومات حصرية! 🏷️ تواصل معنا لمعرفة الكوبونات المتاحة.\n\n"
            "We have exclusive offers! 🏷️ Ask us about available coupon codes."
        )

    if any(w in text for w in ("delivery", "توصيل", "شحن", "shipping", "متى يوصل")):
        return (
            "نوصل لجميع مناطق المملكة 🚚\n"
            "التوصيل عادةً خلال 1-3 أيام عمل.\n\n"
            "We deliver across Saudi Arabia 🚚\n"
            "Delivery typically takes 1-3 business days."
        )

    if any(w in text for w in ("human", "agent", "موظف", "مسؤول", "تكلم", "speak")):
        return (
            "بالطبع! سأحولك الآن لأحد موظفينا. ⏳\n\n"
            "Of course! Connecting you with a team member now. ⏳"
        )

    return (
        "شكراً لتواصلك معنا! 😊 كيف أقدر أساعدك؟\n\n"
        "Thanks for reaching out! 😊 How can I assist you?"
    )


# ── Endpoint ─────────────────────────────────────────────────────────────────

@app.post("/ai/respond", response_model=AIResponse)
async def ai_respond(request: AIRequest):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="message field is required and cannot be empty")

    logger.info(f"AI request | tenant={request.tenant} | phone={request.phone} | msg={request.message[:80]}")

    try:
        context = _load_tenant_context(request.tenant, request.tenant_id)
    except Exception as exc:
        logger.warning(f"Could not load tenant context: {exc} — using empty context")
        context = {"store_name": "our store", "products": [], "coupons": [], "policy": {}, "branding": {}}

    system_prompt = _build_system_prompt(context)

    try:
        reply = await _call_llm(system_prompt, request.message)
    except httpx.HTTPError as exc:
        logger.error(f"LLM call failed: {exc} — falling back to rule-based")
        reply = _rule_based_response(request.message)

    model_used = OPENAI_MODEL if OPENAI_API_KEY else "rule-based"
    logger.info(f"AI response | tenant={request.tenant} | model={model_used} | reply={reply[:80]}")

    return AIResponse(response=reply, tenant=request.tenant, model=model_used)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ai-engine",
        "llm_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
