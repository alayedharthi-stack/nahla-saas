import os
from sqlalchemy.orm import Session
from fastapi import Depends
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../database')))
from session import SessionLocal
from models import Tenant, User, WhatsAppNumber

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("nahla-backend")

app = FastAPI(title="Nahla SaaS Backend", description="Multi-tenant SaaS API server.")

# Multi-tenant middleware placeholder
# Multi-tenant middleware placeholder
@app.middleware("http")
async def multi_tenant_middleware(request: Request, call_next):
    # Example: Extract tenant from headers or subdomain
    tenant_id = request.headers.get("X-Tenant-ID", "default")
    request.state.tenant_id = tenant_id
    logger.info(f"Request for tenant: {tenant_id}")
    response = await call_next(request)
    return response

# Dependency for DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/health")
async def health():
    """Health check endpoint."""
    logger.info("Health check requested.")
    return {"status": "ok"}


@app.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: int, db: Session = Depends(get_db)):
    """Retrieve a single tenant by its numeric ID."""
    logger.info(f"Tenant lookup requested for id={tenant_id}")
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()
    if not tenant:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"id": tenant.id, "name": tenant.name, "domain": tenant.domain, "is_active": tenant.is_active}

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Nahla SaaS Backend API server...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
