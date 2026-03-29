from fastapi import FastAPI
from api.routes import router as analytics_router

app = FastAPI(
    title="Nahla SaaS Analytics Service",
    description="Store metrics, campaign performance, AI performance, and conversation analytics.",
)
app.include_router(analytics_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "analytics-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8010, reload=True)
