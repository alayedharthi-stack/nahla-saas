from models.models import ProductCreate, ProductResponse, ProductSyncResponse
from repositories.data_access import save_product, get_product_by_id


def create_product(payload: ProductCreate) -> ProductResponse:
    saved = save_product(payload)
    return ProductResponse(**saved)


def get_product(product_id: int) -> ProductResponse | None:
    return get_product_by_id(product_id)


def sync_catalog() -> ProductSyncResponse:
    return ProductSyncResponse(status="success", synced=0, message="Catalog sync placeholder.")
