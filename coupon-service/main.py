from fastapi import FastAPI
from api import router as coupon_router

app = FastAPI(
    title="Nahla SaaS Coupon Service",
    description="Coupon generation, policy management, and coupon rule APIs.",
)
app.include_router(coupon_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "coupon-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
