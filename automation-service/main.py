from fastapi import FastAPI
from api.routes import router as automation_router

app = FastAPI(
    title="Nahla SaaS Automation Service",
    description="Automation flows, abandoned cart triggers, and behavior-based actions.",
)
app.include_router(automation_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "automation-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8009, reload=True)
