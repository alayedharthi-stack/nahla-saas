from fastapi import FastAPI
from api import router as order_router

app = FastAPI(
    title="Nahla SaaS Order Service",
    description="Order lifecycle, checkout orchestration, and abandoned cart APIs.",
)
app.include_router(order_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "order-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8004, reload=True)
