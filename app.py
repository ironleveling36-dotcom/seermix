from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Seedr → pCloud Telegram Bot")

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "Seedr → pCloud Telegram Bot is running"

@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "healthy"
