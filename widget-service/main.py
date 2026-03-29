from fastapi import FastAPI
from api import router as widget_router

app = FastAPI(
    title="Nahla SaaS Widget Service",
    description="Store widget settings and branding API.",
)
app.include_router(widget_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "widget-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8007, reload=True)
