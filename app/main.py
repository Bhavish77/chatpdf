from fastapi import FastAPI

from app.config import get_settings

settings = get_settings()

app = FastAPI(title="AskDocs")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}
