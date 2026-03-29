from fastapi import FastAPI
from api import router as campaign_router

app = FastAPI(
    title="Nahla SaaS Campaign Service",
    description="Campaign automation, triggers, and performance APIs.",
)
app.include_router(campaign_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "campaign-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=True)
