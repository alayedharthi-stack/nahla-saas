from fastapi import APIRouter, HTTPException
from models.models import ProductCreate, ProductResponse, ProductSyncResponse
from services.business_logic import create_product as create_product_logic, get_product as get_product_logic, sync_catalog as sync_catalog_logic

router = APIRouter(prefix="/catalog", tags=["catalog"])

@router.post("/products", response_model=ProductResponse)
async def create_product(payload: ProductCreate):
    return create_product_logic(payload)

@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(product_id: int):
    product = get_product_logic(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product

@router.post("/sync", response_model=ProductSyncResponse)
async def sync_catalog():
    return sync_catalog_logic()
