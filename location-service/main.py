from fastapi import FastAPI
from api.routes import router as location_router

app = FastAPI(
    title="Nahla SaaS Location Service",
    description="Location normalization and address parsing for delivery and logistics.",
)
app.include_router(location_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "location-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8012, reload=True)
