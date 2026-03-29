from fastapi import FastAPI
from api.routes import router as conversation_router

app = FastAPI(
    title="Nahla SaaS Conversation Service",
    description="Conversation management, message history, and human handoff API.",
)
app.include_router(conversation_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "conversation-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8008, reload=True)
