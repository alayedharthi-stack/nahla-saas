from models.models import ProductCreate


def save_product(payload: ProductCreate) -> dict:
    return {
        "id": 1,
        "tenant_id": payload.tenant_id,
        "external_id": payload.external_id,
        "sku": payload.sku,
        "title": payload.title,
        "description": payload.description,
        "price": payload.price,
        "metadata": payload.metadata,
    }


def get_product_by_id(product_id: int) -> dict | None:
    if product_id != 1:
        return None
    return {
        "id": 1,
        "tenant_id": 1,
        "external_id": "external-123",
        "sku": "SKU-001",
        "title": "Sample Product",
        "description": "Sample product description.",
        "price": "99.99",
        "metadata": {"category": "demo"},
    }
