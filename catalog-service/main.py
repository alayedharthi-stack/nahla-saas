from fastapi import FastAPI
from api.routes import router as catalog_router

app = FastAPI(
    title="Nahla SaaS Catalog Service",
    description="Product catalog management, sync, and search.",
)
app.include_router(catalog_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "catalog-service"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=True)
